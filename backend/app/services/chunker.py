"""
Smart Chunker - token-based chunking pipeline for RAG ingestion.

Three-step pipeline:
  1. MarkdownNodeParser (external) splits by headings
  2. SmartChunker._merge_small: chunks below min_tokens merge with the shorter neighbor
  3. SmartChunker._split_large: chunks above max_tokens use SentenceSplitter

Token counting:
  All size calculations use tiktoken o200k_base — real BPE token counts,
  no heuristic approximations.
"""

from app.core.logging import get_logger
logger = get_logger(__name__)

# Lazy-loaded tiktoken encoding (thread-safe after init)
_encoding = None


def _get_encoding():
    """Return the shared tiktoken o200k_base encoding (lazy init)."""
    global _encoding
    if _encoding is None:
        import tiktoken
        _encoding = tiktoken.get_encoding("o200k_base")
    return _encoding


def count_content_units(text: str) -> int:
    """Count real tokens via tiktoken o200k_base."""
    return len(_get_encoding().encode(text))


def custom_tokenizer(text: str) -> list:
    """Return token ID list (for SentenceSplitter tokenizer param)."""
    return _get_encoding().encode(text)


class SmartChunker:
    """LlamaIndex-compatible transformation: merge small chunks, split large ones.

    Usage:
        transformations = [MarkdownNodeParser(), SmartChunker(512, 100, 55)]
    """

    def __init__(self, max_units: int = 512, min_units: int = 100, overlap: int = 55):
        self.max_units = max_units
        self.min_units = min_units
        self._overlap = overlap

        # Lazy-init splitter (avoid heavy import at module level)
        self._splitter = None

    def _ensure_splitter(self):
        if self._splitter is not None:
            return
        from llama_index.core.node_parser import SentenceSplitter
        self._splitter = SentenceSplitter(
            chunk_size=self.max_units,
            chunk_overlap=self._overlap,
            tokenizer=custom_tokenizer,
        )

    # LlamaIndex transformation interface
    def __call__(self, nodes, **kwargs):
        merged = self._merge_small(nodes)
        return self._split_large(merged)

    # ------------------------------------------------------------------
    # Step 2: Merge small chunks
    # ------------------------------------------------------------------

    def _merge_small(self, nodes):
        """Merge chunks < min_units with their shorter neighbor (direct concat)."""
        result = list(nodes)
        i = 0
        while i < len(result):
            text = getattr(result[i], 'text', '') or ''
            if count_content_units(text) < self.min_units:
                left_size = (
                    count_content_units(getattr(result[i - 1], 'text', '') or '')
                    if i > 0 else float('inf')
                )
                right_size = (
                    count_content_units(getattr(result[i + 1], 'text', '') or '')
                    if i + 1 < len(result) else float('inf')
                )

                if left_size <= right_size and i > 0:
                    result[i - 1] = self._concat_nodes(result[i - 1], result[i])
                    result.pop(i)
                    # Don't increment i — recheck current position (new node)
                elif i + 1 < len(result):
                    result[i] = self._concat_nodes(result[i], result[i + 1])
                    result.pop(i + 1)
                    # Recheck current position
                else:
                    i += 1  # Only node, can't merge
            else:
                i += 1
        return result

    # ------------------------------------------------------------------
    # Step 3: Split large chunks
    # ------------------------------------------------------------------

    def _split_large(self, nodes):
        """Split chunks > max_units at sentence boundaries using SentenceSplitter."""
        from llama_index.core.schema import TextNode

        self._ensure_splitter()
        result = []
        for node in nodes:
            text = getattr(node, 'text', '') or ''
            if count_content_units(text) <= self.max_units:
                result.append(node)
                continue

            chunks = self._splitter.split_text(text)
            meta = dict(getattr(node, 'metadata', {}) or {})
            for idx, chunk in enumerate(chunks):
                result.append(TextNode(
                    text=chunk,
                    metadata={**meta, "chunk_index": idx},
                ))

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _concat_nodes(a, b):
        """Concatenate two nodes, preserving metadata from the first."""
        from llama_index.core.schema import TextNode
        text_a = getattr(a, 'text', '') or ''
        text_b = getattr(b, 'text', '') or ''
        meta = dict(getattr(a, 'metadata', {}) or {})
        return TextNode(text=text_a + text_b, metadata=meta)
