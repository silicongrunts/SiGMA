"""
RAG Service - Production RAG with LlamaIndex.

Architecture:
  1. SentenceSplitter — recursive chunking with overlap
  2. ChromaVectorStore + VectorStoreIndex — persistent HNSW via LlamaIndex
  3. BM25 scorer — in-memory per-project, lazy-loaded with LRU eviction
  4. Reciprocal Rank Fusion (vector + BM25)
  5. CrossEncoder reranker (sentence-transformers)

API surface:
  - start() / stop() — call at backend startup/shutdown
  - index_document(project_id, doc_id, content, title, description)
  - remove_document(project_id, doc_id)
  - search(project_id, query, top_k) -> List[SearchResult]
"""
import asyncio
import hashlib
import json
import os
import math
import re
import threading
from dataclasses import dataclass
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, List, Optional

from llama_index.core.base.embeddings.base import BaseEmbedding
from pydantic import PrivateAttr

from app.core.atomic_file import atomic_write_text
from app.core.config import settings
from app.core.exceptions import RAGIndexModelMismatchError
from app.core.model_config import ModelEndpoint, get_model_endpoint

from app.core.logging import get_logger
logger = get_logger(__name__)

MAX_CACHED_PROJECTS = 20
EMBED_BATCH_SIZE = 100
RAG_INDEX_METADATA_VERSION = 1


def _resolve_model_path(model_name: str, source: str, hf_endpoint: str) -> str:
    """Resolve local model path from the configured source.

    - modelscope: download via ModelScope SDK, return local cache path.
    - huggingface (default): optionally set HF_ENDPOINT mirror, return model name.

    HF_ENDPOINT is a process-wide env var consumed by huggingface_hub /
    transformers / sentence-transformers. To avoid state from a previous
    HuggingFace-source call leaking into a ModelScope-source call (and
    vice versa), we explicitly pop it whenever source != huggingface.
    """
    if source == "modelscope":
        # Clean up any HF_ENDPOINT left behind by a prior HF-source resolve
        # so downstream libraries (docling, transformers, ...) don't
        # accidentally route through the HF mirror when the user has
        # explicitly switched this model to ModelScope.
        os.environ.pop("HF_ENDPOINT", None)
        try:
            from modelscope.hub.snapshot_download import snapshot_download
        except ImportError:
            raise RuntimeError(
                "modelscope package is required for source='modelscope'. "
                "Install it with: pip install modelscope"
            )
        logger.info(f"Model source: ModelScope — downloading {model_name}")
        return snapshot_download(model_name)

    # huggingface (default)
    if hf_endpoint:
        logger.info(f"Model source: HuggingFace mirror — {hf_endpoint}")
        os.environ["HF_ENDPOINT"] = hf_endpoint
    return model_name


@dataclass
class SearchChunk:
    """A single matched chunk with its similarity score."""
    doc_id: str
    score: float
    chunk_text: str
    line_start: int = 0


class LiteLLMEmbeddingModel(BaseEmbedding):
    """LiteLLM-backed embedding model with the methods RAG needs."""

    _endpoint: ModelEndpoint = PrivateAttr()

    def __init__(self, endpoint: ModelEndpoint):
        super().__init__(model_name=endpoint.model)
        self._endpoint = endpoint

    def _get_text_embedding(self, text: str):
        return self._embed([text])[0]

    def _get_text_embeddings(self, texts: list[str]):
        return self._embed(texts)

    def _get_query_embedding(self, query: str):
        return self._embed([query])[0]

    async def _aget_query_embedding(self, query: str):
        return await asyncio.to_thread(self._get_query_embedding, query)

    def _embed(self, texts):
        import litellm

        response = litellm.embedding(
            model=self._endpoint.litellm_model,
            input=texts,
            **self._endpoint.litellm_kwargs(),
        )
        payload = _to_dict(response)
        rows = payload.get("data") or []
        rows.sort(key=lambda row: _to_dict(row).get("index", 0))
        embeddings = [_to_dict(row).get("embedding") for row in rows]
        if len(embeddings) != len(texts) or any(e is None for e in embeddings):
            raise RuntimeError("Embedding provider returned malformed embedding data")
        return embeddings


class LiteLLMReranker:
    """LiteLLM-backed reranker with the CrossEncoder.predict() interface."""

    def __init__(self, endpoint: ModelEndpoint):
        self.endpoint = endpoint

    def predict(self, pairs):
        import litellm
        import numpy as np

        if not pairs:
            return np.array([])
        query = pairs[0][0]
        documents = [p[1] for p in pairs]

        response = litellm.rerank(
            model=self.endpoint.litellm_model,
            query=query,
            documents=documents,
            top_n=len(documents),
            **self.endpoint.litellm_kwargs(),
        )
        payload = _to_dict(response)

        scores = np.zeros(len(pairs))
        for result in payload.get("results") or []:
            row = _to_dict(result)
            index = row.get("index")
            if index is None or index >= len(pairs):
                continue
            scores[index] = row.get("relevance_score", row.get("score", 0.0))
        return scores


def _to_dict(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        converted = value.to_dict()
        return _plain_value(converted) if isinstance(converted, dict) else {}
    if hasattr(value, "model_dump"):
        converted = value.model_dump()
        return _plain_value(converted) if isinstance(converted, dict) else {}
    if hasattr(value, "__dict__"):
        return {k: _plain_value(v) for k, v in vars(value).items() if not k.startswith("_")}
    return {}


def _plain_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _plain_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain_value(item) for item in value]
    if hasattr(value, "to_dict"):
        return _plain_value(value.to_dict())
    if hasattr(value, "model_dump"):
        return _plain_value(value.model_dump())
    if hasattr(value, "__dict__"):
        return {k: _plain_value(v) for k, v in vars(value).items() if not k.startswith("_")}
    return value


def _line_number_for_chunk(full_text: str, chunk_text: str) -> int:
    """Best-effort 0-indexed line number for a chunk within full_text."""
    if not full_text or not chunk_text:
        return 0
    needle = chunk_text[:200].strip()
    if not needle:
        return 0
    pos = full_text.find(needle)
    if pos < 0:
        return 0
    return full_text.count("\n", 0, pos)


class RAGService:
    """LlamaIndex-powered RAG with hybrid search + reranking."""

    def __init__(self):
        self._embed_model = None
        self._embedding_identity = None
        self._reranker = None
        self._splitter = None
        self._initialized = False
        self._projects = {}           # project_id -> _ProjectState
        self._project_lru = OrderedDict()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rag")
        self._projects_lock = threading.Lock()
        self._init_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _do_init(self):
        """Load models (heavy, call once at startup). Thread-safe via double-check locking."""
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return

            from llama_index.core.node_parser import MarkdownNodeParser
            from app.services.chunker import SmartChunker

            # ── Embedding model ──────────────────────────────────────────
            embedding_endpoint = get_model_endpoint("embedding")
            query_instruction = settings.RAG_QUERY_INSTRUCTION or None
            if (
                not query_instruction
                and embedding_endpoint.model
                and "harrier" in embedding_endpoint.model.lower()
            ):
                query_instruction = (
                    "Instruct: Given a web search query, retrieve relevant passages "
                    "that answer the query\nQuery: "
                )
            self._embedding_identity = self._build_embedding_identity(
                embedding_endpoint,
                query_instruction=query_instruction,
            )
            if embedding_endpoint.is_local:
                # Local embedding via HuggingFace
                from llama_index.embeddings.huggingface import HuggingFaceEmbedding

                model_path = _resolve_model_path(
                    embedding_endpoint.model,
                    embedding_endpoint.source,
                    embedding_endpoint.hf_endpoint,
                )

                if query_instruction:
                    logger.info(f"RAG: Query instruction enabled ({len(query_instruction)} chars)")

                logger.info(f"RAG: Loading local embedding model {model_path}...")
                self._embed_model = HuggingFaceEmbedding(
                    model_name=model_path,
                    query_instruction=query_instruction,
                )
                logger.info("RAG: Local embedding model loaded")
            else:
                if not embedding_endpoint.model:
                    raise ValueError("EMBEDDING_MODEL is required for cloud embedding")
                logger.info(
                    f"RAG: Loading cloud embedding model {embedding_endpoint.litellm_model}"
                )
                self._embed_model = LiteLLMEmbeddingModel(embedding_endpoint)
                logger.info("RAG: Cloud embedding model ready")

            # ── Smart chunking ───────────────────────────────────────────
            self._md_parser = MarkdownNodeParser()
            self._smart_chunker = SmartChunker(
                max_units=settings.RAG_CHUNK_MAX_UNITS,
                min_units=settings.RAG_CHUNK_MIN_UNITS,
                overlap=settings.RAG_CHUNK_OVERLAP_UNITS,
            )
            logger.info(
                f"RAG: Smart chunker ready (max={settings.RAG_CHUNK_MAX_UNITS}, "
                f"min={settings.RAG_CHUNK_MIN_UNITS}, overlap={settings.RAG_CHUNK_OVERLAP_UNITS})"
            )

            # ── Reranker ─────────────────────────────────────────────────
            reranker_endpoint = get_model_endpoint("rerank")
            if settings.RAG_RERANKER_ENABLED and reranker_endpoint.model:
                if reranker_endpoint.is_local:
                    try:
                        from sentence_transformers import CrossEncoder
                        model_path = _resolve_model_path(
                            reranker_endpoint.model,
                            reranker_endpoint.source,
                            reranker_endpoint.hf_endpoint,
                        )
                        logger.info(f"RAG: Loading local reranker {model_path}...")
                        self._reranker = CrossEncoder(model_path)
                        logger.info("RAG: Local reranker loaded")
                    except Exception as e:
                        logger.warning("RAG: Reranker disabled: %s", e, exc_info=True)
                        self._reranker = None
                else:
                    self._reranker = LiteLLMReranker(reranker_endpoint)
                    logger.info(f"RAG: Cloud reranker ready ({reranker_endpoint.litellm_model})")
            else:
                self._reranker = None
                if not reranker_endpoint.model:
                    logger.info("RAG: Reranker disabled — RERANKER_MODEL not configured")

            self._initialized = True
            logger.info("RAG service fully initialized")

    async def start(self):
        """Called at backend startup. Loads models in thread pool."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._do_init)

    async def stop(self):
        self._executor.shutdown(wait=False)

    async def reset_project_index(self, project_id: str):
        """Delete the entire ChromaDB collection for a project so it can be recreated."""
        await asyncio.get_event_loop().run_in_executor(
            self._executor, self._sync_reset_project, project_id
        )

    def _sync_reset_project(self, project_id: str):
        import chromadb
        self._ensure_init()
        chroma_dir = settings.get_sigma_path(project_id) / "chroma"
        if not chroma_dir.exists():
            self._write_index_metadata(project_id)
            return
        client = chromadb.PersistentClient(path=str(chroma_dir))
        try:
            client.delete_collection(name="library")
            logger.info(f"RAG: deleted collection for project {project_id}")
        except Exception:
            logger.debug("RAG collection did not need deletion for project %s", project_id, exc_info=True)
        self._write_index_metadata(project_id)
        # Evict from cache so next access creates fresh collection
        self._evict_project(project_id)

    # ------------------------------------------------------------------
    # Orphan cleanup
    # ------------------------------------------------------------------

    async def cleanup_orphans(self, project_id: str, valid_doc_ids: set):
        """Remove orphan chunks, or reset collection if library is empty."""
        if not valid_doc_ids:
            # Library is empty — delete collection via ChromaDB API (safe across processes).
            # Cannot shutil.rmtree the chroma directory here: the Huey worker process
            # may have an open SQLite handle, and removing the directory prevents
            # WAL/journal file creation → "readonly database" errors.
            await self.reset_project_index(project_id)
        else:
            await asyncio.get_event_loop().run_in_executor(
                self._executor, self._sync_cleanup_orphans, project_id, valid_doc_ids
            )

    def _sync_cleanup_orphans(self, project_id: str, valid_doc_ids: set):
        """Scan all chunks and remove those whose doc_id is not in valid set."""
        self._ensure_init()
        state = self._get_project(project_id)
        collection = state.vector_store._collection

        if collection.count() == 0:
            return

        all_records = collection.get(include=["metadatas"])
        orphan_ids = []
        for rid, meta in zip(all_records["ids"], all_records["metadatas"]):
            chunk_doc_id, _ = self._extract_chunk_identity(meta or {})
            if chunk_doc_id and chunk_doc_id not in valid_doc_ids:
                orphan_ids.append(rid)

        if orphan_ids:
            collection.delete(ids=orphan_ids)
            logger.info(
                f"RAG cleanup: removed {len(orphan_ids)} orphan chunks in project {project_id}"
            )

        # Clean BM25 in-memory nodes
        state.all_nodes = [
            n for n in state.all_nodes
            if self._extract_chunk_identity(n.metadata)[0] in valid_doc_ids
        ]
        self._invalidate_bm25(state)

    def _ensure_init(self):
        if not self._initialized:
            self._do_init()

    @staticmethod
    def _build_embedding_identity(
        endpoint: ModelEndpoint,
        *,
        query_instruction: str | None,
    ) -> dict:
        payload = {
            "version": RAG_INDEX_METADATA_VERSION,
            "role": "embedding",
            "model": endpoint.model,
            "provider": endpoint.provider,
            "api_base": endpoint.api_base,
            "source": endpoint.source,
            "hf_endpoint": endpoint.hf_endpoint,
            "extra": endpoint.extra,
            "query_instruction": query_instruction or "",
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return {
            "schema_version": RAG_INDEX_METADATA_VERSION,
            "embedding": payload,
            "identity_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "display_model": endpoint.litellm_model if not endpoint.is_local else endpoint.model,
        }

    def _metadata_path(self, project_id: str) -> Path:
        return settings.get_sigma_path(project_id) / "rag_index_metadata.json"

    def _read_index_metadata(self, project_id: str) -> dict | None:
        path = self._metadata_path(project_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_index_metadata(self, project_id: str) -> None:
        if not self._embedding_identity:
            return
        path = self._metadata_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path,
            json.dumps(self._embedding_identity, ensure_ascii=False, indent=2),
        )

    def _ensure_index_metadata(self, project_id: str, collection) -> None:
        current = self._embedding_identity or {}
        current_hash = current.get("identity_hash", "")
        if not current_hash:
            return

        stored = self._read_index_metadata(project_id)
        stored_hash = (stored or {}).get("identity_hash", "")
        metadata = dict(getattr(collection, "metadata", None) or {})
        collection_hash = metadata.get("embedding_identity_hash", "")
        if not stored_hash and collection_hash == current_hash:
            self._write_index_metadata(project_id)
            stored_hash = current_hash
        chunk_count = collection.count()

        if chunk_count > 0 and stored_hash != current_hash:
            raise RAGIndexModelMismatchError(
                current_model=current.get("display_model", ""),
                indexed_model=(stored or {}).get("display_model", "unknown"),
            )

        if chunk_count == 0 and stored_hash != current_hash:
            self._write_index_metadata(project_id)

        if metadata.get("embedding_identity_hash") == current_hash:
            return
        metadata.update({
            "hnsw:space": "cosine",
            "embedding_identity_hash": current_hash,
            "embedding_model": current.get("display_model", ""),
            "rag_index_metadata_version": RAG_INDEX_METADATA_VERSION,
        })
        try:
            collection.modify(metadata=metadata)
        except Exception as exc:
            logger.warning(
                "RAG: failed to update collection metadata for %s: %s",
                project_id,
                exc,
                exc_info=True,
            )

    def _evict_project(self, project_id: str) -> None:
        """Remove a project's cached state, forcing recreation on next access."""
        with self._projects_lock:
            state = self._projects.pop(project_id, None)
            self._project_lru.pop(project_id, None)
        if state:
            state.all_nodes = []
            self._invalidate_bm25(state)
        logger.info("RAG: evicted stale cache for project %s", project_id)

    def evict_project(self, project_id: str) -> None:
        """Public API for evicting a project's cached RAG state."""
        self._evict_project(project_id)

    # ------------------------------------------------------------------
    # Per-project state with LRU eviction
    # ------------------------------------------------------------------

    class _ProjectState:
        __slots__ = ("vector_store", "index", "all_nodes", "bm25_index")

        def __init__(self, vector_store, index):
            self.vector_store = vector_store  # ChromaVectorStore
            self.index = index                # VectorStoreIndex
            self.all_nodes = []
            self.bm25_index = None

    def _get_project(self, project_id: str) -> "_ProjectState":
        """Get or create project state via LlamaIndex abstractions. Thread-safe."""
        self._ensure_init()

        with self._projects_lock:
            if project_id in self._projects:
                self._project_lru.move_to_end(project_id)
                return self._projects[project_id]

        # --- Slow path: create state outside lock ---
        import chromadb
        from llama_index.vector_stores.chroma import ChromaVectorStore
        from llama_index.core import VectorStoreIndex, StorageContext

        chroma_dir = settings.get_sigma_path(project_id) / "chroma"
        chroma_dir.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(chroma_dir))
        collection = client.get_or_create_collection(
            name="library",
            metadata={
                "hnsw:space": "cosine",
                "embedding_identity_hash": (self._embedding_identity or {}).get("identity_hash", ""),
                "embedding_model": (self._embedding_identity or {}).get("display_model", ""),
                "rag_index_metadata_version": RAG_INDEX_METADATA_VERSION,
            },
        )
        self._ensure_index_metadata(project_id, collection)

        vector_store = ChromaVectorStore(chroma_collection=collection)
        storage_context = StorageContext.from_defaults(vector_store=vector_store)
        index = VectorStoreIndex.from_vector_store(
            vector_store,
            embed_model=self._embed_model,
            transformations=[self._md_parser, self._smart_chunker],
            storage_context=storage_context,
        )

        state = self._ProjectState(vector_store, index)
        with self._projects_lock:
            # Double-check: another thread may have created it
            if project_id in self._projects:
                return self._projects[project_id]
            # Evict oldest project if over limit
            while len(self._projects) >= MAX_CACHED_PROJECTS:
                oldest_id, _ = self._project_lru.popitem(last=False)
                evicted = self._projects.pop(oldest_id, None)
                if evicted:
                    evicted.all_nodes = []
                    self._invalidate_bm25(evicted)
                    logger.info("RAG: evicted project %s from cache", oldest_id)
            self._projects[project_id] = state
            self._project_lru[project_id] = True

        logger.info("RAG project %s: %d chunks", project_id, collection.count())
        return state

    def _chunk_count(self, state: "_ProjectState") -> int:
        """Get chunk count from the underlying ChromaDB collection."""
        return state.vector_store._collection.count()

    # ------------------------------------------------------------------
    # Internal: chunk metadata helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_chunk_identity(meta: dict) -> tuple[str | None, int | None]:
        """Extract (doc_id, doc_revision) from a ChromaDB chunk metadata record.

        LlamaIndex's ChromaVectorStore adapter (v0.5.x) serializes node
        metadata into the ``_node_content`` JSON column instead of top-level
        ChromaDB metadata columns, and ChromaDB serializes Python ``None``
        as the string ``"None"``. This helper normalizes all three storage
        shapes (top-level column, ``"None"`` sentinel, embedded JSON) into a
        single authoritative lookup so callers do not need their own
        fallback chains.
        """
        doc_id = meta.get("doc_id")
        revision = meta.get("doc_revision")

        if doc_id == "None":
            doc_id = None

        if not doc_id:
            try:
                node = json.loads(meta.get("_node_content", "{}"))
                node_meta = node.get("metadata", {})
                doc_id = node_meta.get("doc_id")
                if revision is None:
                    revision = node_meta.get("doc_revision")
            except (json.JSONDecodeError, AttributeError):
                pass

        try:
            revision_int = int(revision) if revision is not None else None
        except (TypeError, ValueError):
            revision_int = None
        return doc_id, revision_int

    # ------------------------------------------------------------------
    # Internal: purge all ChromaDB chunks for a doc_id
    # ------------------------------------------------------------------

    def _purge_doc_chunks(self, state, doc_id, max_revision: int | None = None):
        """Remove chunks belonging to doc_id.

        When ``max_revision`` is provided, only chunks whose revision is
        missing or ``<= max_revision`` are removed. This prevents a stale
        indexing task from deleting chunks produced by a newer task for
        the same document.

        Failures are logged but do not raise: purge is best-effort, and the
        subsequent indexing pass will write fresh chunks regardless. The
        worst-case residue is a few orphan chunks, which the periodic
        ``cleanup_orphans`` sweep will reclaim.
        """
        collection = state.vector_store._collection

        try:
            # Single scan: top-level metadata, "None" sentinel, and embedded
            # _node_content JSON are all normalized by _extract_chunk_identity.
            all_records = collection.get(include=["metadatas"])
            to_delete = []
            for rid, meta in zip(all_records["ids"], all_records["metadatas"]):
                chunk_doc_id, chunk_rev = self._extract_chunk_identity(meta or {})
                if chunk_doc_id != doc_id:
                    continue
                if max_revision is not None and chunk_rev is not None and chunk_rev > max_revision:
                    continue
                to_delete.append(rid)

            if to_delete:
                collection.delete(ids=to_delete)
                if max_revision is not None:
                    logger.info(
                        "RAG purge: removed %d chunks for doc %s up to revision %s",
                        len(to_delete), doc_id, max_revision,
                    )
                else:
                    logger.info(
                        "RAG purge: removed %d chunks for doc %s",
                        len(to_delete), doc_id,
                    )
        except Exception as e:
            logger.warning(
                "RAG purge: scan/delete failed for doc %s: %s",
                doc_id, e, exc_info=True,
            )

        # Clean in-memory BM25 nodes with the same predicate
        def _node_matches(n) -> bool:
            node_doc_id, node_revision = self._extract_chunk_identity(n.metadata)
            if node_doc_id != doc_id:
                return False
            if max_revision is None:
                return True
            try:
                return node_revision is None or node_revision <= max_revision
            except (TypeError, ValueError):
                return True

        state.all_nodes = [n for n in state.all_nodes if not _node_matches(n)]
        self._invalidate_bm25(state)

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    @staticmethod
    def _is_stale_collection_error(exc: Exception) -> bool:
        """Check if an exception indicates a stale/missing ChromaDB collection."""
        msg = str(exc).lower()
        return "collection" in msg and ("does not exist" in msg or "not found" in msg)

    def _sync_index(self, project_id, doc_id, content, title, description,
                    progress_callback: Optional[Callable[[], None]] = None,
                    cancel_event: Optional[asyncio.Event] = None,
                    should_continue: Optional[Callable[[], bool]] = None,
                    doc_revision: int | None = None,
                    _stale_cache_retried: bool = False):
        """Index a document using manual split -> batch embed -> batch store pipeline.

        Thread-safe. Runs inside ThreadPoolExecutor.

        Args:
            progress_callback: Optional callable invoked between embedding batches.
                Used to heartbeat the owning background task.
            should_continue: Optional callable checked before destructive writes.
            doc_revision: Document revision represented by the chunks being written.
            _stale_cache_retried: Internal flag to prevent double retry on stale cache.
        """
        from llama_index.core import Document

        self._ensure_init()

        try:
            state = self._get_project(project_id)
        except Exception as e:
            if _stale_cache_retried or not self._is_stale_collection_error(e):
                raise
            self._evict_project(project_id)
            state = self._get_project(project_id)

        full_text = f"{title}\n\n{description}\n\n{content}" if title else content
        if not full_text.strip():
            return

        try:
            if should_continue and not should_continue():
                logger.info("Indexing skipped for stale/cancelled document %s", doc_id)
                return

            # Purge old chunks for this document
            self._purge_doc_chunks(state, doc_id, max_revision=doc_revision)

            # ── 1. Split (single pass, shared by vector store + BM25) ────
            doc_metadata = {"doc_id": doc_id}
            if doc_revision is not None:
                doc_metadata["doc_revision"] = doc_revision
            doc = Document(text=full_text, doc_id=doc_id, metadata=doc_metadata)
            nodes = self._md_parser.get_nodes_from_documents([doc])
            nodes = self._smart_chunker(nodes)

            # ── 2. Metadata fix: set source relationship on every node ──
            # TextNode.ref_doc_id is a read-only property backed by the
            # SOURCE relationship.  Setting it via relationships ensures
            # ChromaDB metadata gets the correct doc_id (not "None").
            from llama_index.core.schema import NodeRelationship, RelatedNodeInfo
            for n in nodes:
                n.relationships[NodeRelationship.SOURCE] = RelatedNodeInfo(node_id=doc_id)
                n.metadata["doc_id"] = doc_id
                if doc_revision is not None:
                    n.metadata["doc_revision"] = doc_revision
                n.metadata["line_start"] = _line_number_for_chunk(full_text, n.get_content())

            # ── 3. Batch embed + progress updates ───────────────────────
            for i in range(0, len(nodes), EMBED_BATCH_SIZE):
                if cancel_event and cancel_event.is_set():
                    logger.info("Indexing cancelled for %s, aborting mid-batch", doc_id)
                    return
                if should_continue and not should_continue():
                    logger.info("Indexing stopped for stale/cancelled document %s", doc_id)
                    return
                batch = nodes[i:i + EMBED_BATCH_SIZE]
                texts = [n.get_content() for n in batch]
                embeddings = self._embed_model.get_text_embedding_batch(texts)
                for node, emb in zip(batch, embeddings):
                    node.embedding = emb

                # Report progress between batches
                if progress_callback:
                    try:
                        progress_callback()
                    except Exception as e:
                        logger.warning("Progress callback failed for %s: %s", doc_id, e, exc_info=True)

            # ── 4. Store in ChromaDB (embeddings already set on nodes) ───
            if should_continue and not should_continue():
                logger.info("Indexing skipped before storing stale document %s", doc_id)
                return
            state.vector_store.add(nodes)

            # ── 5. Update BM25 in-memory nodes (reuse same split) ────────
            state.all_nodes = [n for n in state.all_nodes if n.metadata.get("doc_id") != doc_id]
            state.all_nodes.extend(nodes)
            self._invalidate_bm25(state)

            logger.info("Indexed %s: %d chunks", doc_id, len(nodes))

        except Exception as e:
            if self._is_stale_collection_error(e) and not _stale_cache_retried:
                # Stale ChromaDB cache — evict and retry once (does not count as failure)
                logger.warning(
                    "Stale ChromaDB collection for project %s, evicting cache and retrying",
                    project_id,
                )
                self._evict_project(project_id)
                # Retry the full pipeline once
                self._sync_index(
                    project_id, doc_id, content, title, description,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                    should_continue=should_continue,
                    doc_revision=doc_revision,
                    _stale_cache_retried=True,
                )
                return

            raise

    async def index_document(self, project_id, doc_id, content, title="", description="",
                             progress_callback: Optional[Callable[[], None]] = None,
                             cancel_event: Optional[asyncio.Event] = None,
                             should_continue: Optional[Callable[[], bool]] = None,
                             doc_revision: int | None = None):
        if not content.strip() and not title.strip():
            return
        await asyncio.get_event_loop().run_in_executor(
            self._executor, self._sync_index,
            project_id, doc_id, content, title, description,
            progress_callback, cancel_event, should_continue, doc_revision,
        )

    # ------------------------------------------------------------------
    # Remove
    # ------------------------------------------------------------------

    def _sync_remove(self, project_id, doc_id):
        self._ensure_init()
        state = self._get_project(project_id)
        self._purge_doc_chunks(state, doc_id)

    async def remove_document(self, project_id, doc_id):
        await asyncio.get_event_loop().run_in_executor(
            self._executor, self._sync_remove, project_id, doc_id
        )

    # ------------------------------------------------------------------
    # Search: vector + BM25 → RRF → optional rerank → return chunks
    # ------------------------------------------------------------------

    @staticmethod
    def _effective_candidate_pool_size(top_k: int) -> int:
        """Return a candidate pool large enough to satisfy the final top_k."""
        return max(top_k, settings.RAG_CANDIDATE_POOL_SIZE)

    @staticmethod
    def _cap_per_doc(chunks: List["SearchChunk"], max_per_doc: int,
                     pool_size: int) -> List["SearchChunk"]:
        """Limit each document to at most *max_per_doc* chunks while filling
        the pool to *pool_size* from remaining candidates.

        Chunks are assumed pre-sorted by relevance.  For each doc we keep the
        top *max_per_doc* chunks; lower-ranked chunks from that doc are
        discarded, allowing chunks from other documents to fill the pool.
        """
        if max_per_doc <= 0 or not chunks:
            return chunks[:pool_size]

        accepted: list[SearchChunk] = []
        doc_counts: dict[str, int] = {}

        for chunk in chunks:
            if len(accepted) >= pool_size:
                break
            count = doc_counts.get(chunk.doc_id, 0)
            if count >= max_per_doc:
                continue
            accepted.append(chunk)
            doc_counts[chunk.doc_id] = count + 1

        return accepted

    @staticmethod
    def _tokenize_for_bm25(text: str) -> list[str]:
        """Language-tolerant tokenizer: word tokens plus CJK character bigrams."""
        if not text:
            return []
        lowered = text.lower()
        tokens = re.findall(r"[a-z0-9_]+", lowered)
        cjk_chars = re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", lowered)
        tokens.extend(cjk_chars)
        tokens.extend(a + b for a, b in zip(cjk_chars, cjk_chars[1:]))
        return tokens

    @staticmethod
    def _invalidate_bm25(state) -> None:
        if hasattr(state, "bm25_index"):
            state.bm25_index = None

    def _ensure_bm25_index(self, state) -> dict:
        if getattr(state, "bm25_index", None) is not None:
            return state.bm25_index

        if not state.all_nodes:
            from llama_index.core.schema import TextNode

            collection = state.vector_store._collection
            records = collection.get(include=["documents", "metadatas"])
            nodes = []
            for i, node_id in enumerate(records["ids"]):
                doc = (records["documents"] or [])[i] or ""
                meta = dict((records["metadatas"] or [])[i] or {})
                doc_id, revision = self._extract_chunk_identity(meta)
                if doc_id:
                    meta["doc_id"] = doc_id
                if revision is not None:
                    meta["doc_revision"] = revision
                nodes.append(TextNode(id_=node_id, text=doc, metadata=meta))
            state.all_nodes = nodes

        entries = []
        for node in state.all_nodes:
            doc_id, _ = self._extract_chunk_identity(node.metadata)
            if not doc_id:
                continue
            tokens = self._tokenize_for_bm25(node.get_content())
            if tokens:
                entries.append({
                    "node": node,
                    "doc_id": doc_id,
                    "tokens": tokens,
                    "term_counts": Counter(tokens),
                })

        state.bm25_index = {"entries": entries}
        return state.bm25_index

    def _bm25_search(self, state, query: str, fetch_k: int,
                     allowed_doc_ids=None) -> list[SearchChunk]:
        """BM25 scorer over cached RAG chunk tokens."""
        bm25_index = self._ensure_bm25_index(state)
        allowed = set(allowed_doc_ids) if allowed_doc_ids is not None else None
        entries = [
            entry for entry in bm25_index["entries"]
            if allowed is None or entry["doc_id"] in allowed
        ]

        query_tokens = self._tokenize_for_bm25(query)
        if not entries or not query_tokens:
            return []

        n_docs = len(entries)
        avg_len = sum(len(entry["tokens"]) for entry in entries) / max(1, n_docs)
        df = Counter()
        for entry in entries:
            for token in set(entry["tokens"]):
                df[token] += 1

        k1 = 1.5
        b = 0.75
        scored = []
        for entry in entries:
            node = entry["node"]
            doc_len = len(entry["tokens"])
            term_counts = entry["term_counts"]
            score = 0.0
            for token in query_tokens:
                freq = term_counts.get(token, 0)
                if not freq:
                    continue
                idf = math.log(1 + (n_docs - df[token] + 0.5) / (df[token] + 0.5))
                denom = freq + k1 * (1 - b + b * doc_len / max(avg_len, 1e-9))
                score += idf * (freq * (k1 + 1)) / denom
            if score > 0:
                scored.append(SearchChunk(
                    doc_id=node.metadata.get("doc_id", ""),
                    score=float(score),
                    chunk_text=node.get_content(),
                    line_start=int(node.metadata.get("line_start") or 0),
                ))

        scored.sort(key=lambda chunk: chunk.score, reverse=True)
        return scored[:fetch_k]

    @staticmethod
    def _rrf_merge(vector_results: list[SearchChunk],
                   bm25_results: list[SearchChunk],
                   fetch_k: int) -> list[SearchChunk]:
        if not bm25_results:
            return vector_results[:fetch_k]

        merged: dict[tuple[str, str], SearchChunk] = {}
        scores: dict[tuple[str, str], float] = {}
        rank_constant = 60

        for result_set in (vector_results, bm25_results):
            for rank, chunk in enumerate(result_set, start=1):
                key = (chunk.doc_id, chunk.chunk_text[:300])
                if key not in merged:
                    merged[key] = SearchChunk(
                        doc_id=chunk.doc_id,
                        score=0.0,
                        chunk_text=chunk.chunk_text,
                        line_start=chunk.line_start,
                    )
                    scores[key] = 0.0
                scores[key] += 1.0 / (rank_constant + rank)

        ranked = list(merged.values())
        for chunk in ranked:
            key = (chunk.doc_id, chunk.chunk_text[:300])
            chunk.score = scores[key]
        ranked.sort(key=lambda chunk: chunk.score, reverse=True)
        return ranked[:fetch_k]

    def _sync_search(self, project_id, query, top_k, allowed_doc_ids=None) -> List[SearchChunk]:
        from llama_index.core import QueryBundle

        state = self._get_project(project_id)

        if self._chunk_count(state) == 0:
            return []

        fetch_k = self._effective_candidate_pool_size(top_k)

        if allowed_doc_ids is not None:
            # Pre-filter: query ChromaDB directly with doc_id filter
            collection = state.vector_store._collection
            query_embedding = self._embed_model.get_query_embedding(query)
            chroma_results = collection.query(
                query_embeddings=[query_embedding],
                n_results=fetch_k,
                where={"doc_id": {"$in": allowed_doc_ids}},
            )
            vector_results = []
            ids = chroma_results["ids"][0] if chroma_results["ids"] else []
            distances = chroma_results["distances"][0] if chroma_results["distances"] else []
            documents = chroma_results["documents"][0] if chroma_results["documents"] else []
            metadatas = chroma_results["metadatas"][0] if chroma_results["metadatas"] else []
            for i in range(len(ids)):
                vector_results.append(SearchChunk(
                    doc_id=metadatas[i].get("doc_id", ""),
                    score=1.0 - distances[i],  # cosine distance → similarity
                    chunk_text=documents[i],
                    line_start=int(metadatas[i].get("line_start") or 0),
                ))
        else:
            # Standard LlamaIndex retriever path
            retriever = state.index.as_retriever(similarity_top_k=fetch_k)
            query_bundle = QueryBundle(query_str=query)
            nodes = retriever.retrieve(query_bundle)

            vector_results = []
            for n in nodes:
                did = n.node.metadata.get("doc_id")
                if did:
                    vector_results.append(SearchChunk(
                        doc_id=did,
                        score=float(n.score),
                        chunk_text=n.node.text,
                        line_start=int(n.node.metadata.get("line_start") or 0),
                    ))

        try:
            bm25_results = self._bm25_search(state, query, fetch_k, allowed_doc_ids)
        except Exception as e:
            logger.warning("BM25 search failed, using vector results only: %s", e, exc_info=True)
            bm25_results = []

        results = self._rrf_merge(vector_results, bm25_results, fetch_k)

        # Cap per-doc matches before reranking
        results = self._cap_per_doc(results, settings.RAG_MAX_MATCHES_PER_DOC, fetch_k)

        # Rerank with CrossEncoder if available
        if self._reranker and results:
            try:
                pairs = [[query, r.chunk_text[:500]] for r in results]
                scores = self._reranker.predict(pairs)
                for i, score in enumerate(scores):
                    results[i].score = float(score)
                results.sort(key=lambda x: x.score, reverse=True)
                # Re-apply per-doc cap after reranking changes score order
                results = self._cap_per_doc(results, settings.RAG_MAX_MATCHES_PER_DOC, top_k)
                logger.info(
                    f"RAG search '{query[:30]}': reranked {len(pairs)} -> {len(results)}, "
                    f"scores: {[round(r.score, 2) for r in results[:3]]}"
                )
                return results
            except Exception as e:
                logger.warning("Reranker failed, using vector scores: %s", e, exc_info=True)

        results = results[:fetch_k]
        logger.info(
            f"RAG search '{query[:30]}': {len(results)} chunks, "
            f"scores: {[round(r.score, 3) for r in results[:3]]}"
        )
        return results

    async def search(self, project_id, query, top_k=None, allowed_doc_ids=None) -> List[SearchChunk]:
        top_k = top_k or settings.RAG_TOP_K
        return await asyncio.get_event_loop().run_in_executor(
            self._executor, self._sync_search, project_id, query, top_k, allowed_doc_ids
        )


rag_service = RAGService()
