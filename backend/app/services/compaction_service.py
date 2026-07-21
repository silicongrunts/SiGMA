"""Session compaction for LLM request contexts.

Compaction is a context-view transformation:

1. Estimate the exact request that would be sent to the model.
2. If it exceeds the configured threshold, ask the same model to summarize it.
3. Persist or inject a boundary summary so future model calls start there.

Historical messages remain in the database and UI.  Only the messages sent to
the next LLM call are shortened.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import tiktoken

from app.core.config import settings
from app.core.utils import image_dimensions
from app.core.logging import get_logger
from app.database.unit_of_work import UnitOfWork
from app.services.llm_service import llm_service


logger = get_logger(__name__)


IMAGE_HEADER_DECODE_BYTES = 64 * 1024
COMPACT_PROMPT = """You are compacting this SiGMA LLM session so another call can continue with less context.

Create a precise handoff summary. The next model call will receive only the original system prompt, your summary, and later messages.

Your summary must preserve:
- the user's current goal and the latest user request, including exact constraints;
- what has already been done and what remains;
- important decisions, assumptions, file paths, commands, tool results, errors, and verification status;
- pending task lists, plan files, checkpoints, approvals, annotation context, subagent state, and any unfinished work;
- enough detail for a capable LLM to continue without asking the user to repeat context.

Do not invent facts. Do not omit active blockers. Be concise but complete. Prefer structured sections with concrete filenames, IDs, and next steps.

Do not call any tools or functions. Respond with only the summary text."""


PASSIVE_SUMMARY_PREFIX = """This session was compacted automatically because the context exceeded the configured threshold.

Continue the user's latest request using the summary below. Do not ask the user to repeat information already captured here.

"""


ACTIVE_SUMMARY_PREFIX = """The user explicitly requested /compact. This session summary is now the active handoff context.

When the user sends the next request, continue from this summary and the subsequent messages.

"""



def _decode_base64_prefix(payload: str, max_bytes: int) -> bytes:
    """Decode at most enough base64 input to produce max_bytes of binary data."""
    compact = "".join(payload.split())
    if not compact:
        return b""
    chars = ((max_bytes + 2) // 3) * 4
    prefix = compact[:chars]
    prefix += "=" * (-len(prefix) % 4)
    return base64.b64decode(prefix, validate=True)


@dataclass(frozen=True)
class ContextBudget:
    """Resolved context budget for one model role."""

    max_context_length: int
    compact_threshold: int
    response_max_tokens: int
    compact_response_max_tokens: int


@dataclass(frozen=True)
class ContextStats:
    """Token accounting sent to the UI."""

    current_tokens: int
    compact_threshold: int
    max_context_length: int

    def to_dict(self) -> dict[str, int]:
        return {
            "current_tokens": self.current_tokens,
            "compact_threshold": self.compact_threshold,
            "max_context_length": self.max_context_length,
        }


@dataclass(frozen=True)
class CompactionResult:
    """Result of a successful compaction."""

    summary: str
    boundary_content: str
    messages: list[dict]
    stats: ContextStats
    usage: dict | None = None


class CompactionService:
    """Own token estimation and compacted message construction."""

    def __init__(self):
        self._encoding = tiktoken.get_encoding("o200k_base")

    def budget_for_role(self, model_role: str) -> ContextBudget:
        return ContextBudget(
            max_context_length=settings.max_context_length_for_role(model_role),
            compact_threshold=settings.compact_threshold_for_role(model_role),
            response_max_tokens=settings.NORMAL_RESPONSE_MAX_TOKENS,
            compact_response_max_tokens=settings.COMPACT_RESPONSE_MAX_TOKENS,
        )

    def count_tokens_fallback(self, text: str) -> int:
        """Count tokens with the project standard tokenizer."""
        return len(self._encoding.encode(text or ""))

    def estimate_messages_tokens(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> int:
        """Estimate a chat completion request with tiktoken.

        This intentionally includes role names, tool-call JSON, and tool schemas.
        It is still an estimate, but it tracks the full request shape instead of
        summing persisted output tokens.
        """
        total = 0
        for message in messages:
            total += 4
            total += self.count_tokens_fallback(str(message.get("role", "")))
            content = message.get("content")
            if isinstance(content, str):
                total += self.count_tokens_fallback(content)
            elif isinstance(content, list):
                total += self._count_multimodal_tokens(content)
            elif content is not None:
                total += self.count_tokens_fallback(json.dumps(content, ensure_ascii=False))
            for key in ("tool_calls", "tool_call_id", "reasoning_content"):
                value = message.get(key)
                if value:
                    total += self.count_tokens_fallback(json.dumps(value, ensure_ascii=False))
        if tools:
            total += self.count_tokens_fallback(json.dumps(tools, ensure_ascii=False))
        return total

    def _count_multimodal_tokens(self, parts: list[dict]) -> int:
        """Count tokens for a multimodal content list (text + images).

        Text parts use tiktoken.  Image parts use a megapixel-based formula
        instead of tokenizing the raw base64 string.
        """
        total = 0
        for part in parts:
            if not isinstance(part, dict):
                total += self.count_tokens_fallback(json.dumps(part, ensure_ascii=False))
                continue
            ptype = part.get("type", "")
            if ptype == "text":
                total += self.count_tokens_fallback(part.get("text", ""))
            elif ptype == "image_url":
                total += self._estimate_image_url_tokens(
                    part.get("image_url", {}).get("url", "")
                )
            else:
                total += self.count_tokens_fallback(json.dumps(part, ensure_ascii=False))
        return total

    @staticmethod
    def _estimate_image_url_tokens(url: str) -> int:
        """Estimate tokens for an image injected into the conversation context.

        Reads dimensions from the image binary header (no full decode) and
        applies ``int(megapixel * 1000)``.  Returns 0 when the format is
        unrecognised or the data is malformed.
        """
        if not url.startswith("data:"):
            return 0  # HTTP URLs — not produced by SiGMA tools
        # Extract base64 payload after the comma
        comma = url.find(",", 5)
        if comma < 0:
            return 0
        try:
            raw = _decode_base64_prefix(url[comma + 1 :], IMAGE_HEADER_DECODE_BYTES)
        except Exception:
            logger.debug("Failed to decode image data URL header", exc_info=True)
            return 0
        w, h = image_dimensions(raw)
        if w and h:
            return int(w * h / 1_000_000 * 1000)
        return 0

    def stats_for_messages(
        self,
        messages: list[dict],
        *,
        model_role: str,
        tools: list[dict] | None = None,
    ) -> ContextStats:
        budget = self.budget_for_role(model_role)
        return ContextStats(
            current_tokens=self.estimate_messages_tokens(messages, tools),
            compact_threshold=budget.compact_threshold,
            max_context_length=budget.max_context_length,
        )

    def stats_for_messages_incremental(
        self,
        messages: list[dict],
        *,
        model_role: str,
        tools: list[dict] | None = None,
        last_real_input_tokens: int = 0,
        last_real_count_at_index: int = 0,
    ) -> ContextStats:
        """Like ``stats_for_messages`` but uses real LLM tokens when available.

        If *last_real_input_tokens* is non-zero, only the messages added
        since *last_real_count_at_index* are estimated with tiktoken; the
        rest uses the real count.  Falls back to full estimation when no
        real data is available or the message list was replaced (compaction).
        """
        budget = self.budget_for_role(model_role)

        if last_real_input_tokens <= 0 or last_real_count_at_index < 0:
            current_tokens = self.estimate_messages_tokens(messages, tools)
        elif last_real_count_at_index > len(messages):
            current_tokens = self.estimate_messages_tokens(messages, tools)
        elif last_real_count_at_index == len(messages):
            current_tokens = last_real_input_tokens
        else:
            new_messages = messages[last_real_count_at_index:]
            incremental = self.estimate_messages_tokens(new_messages, tools=None)
            current_tokens = last_real_input_tokens + incremental

        return ContextStats(
            current_tokens=current_tokens,
            compact_threshold=budget.compact_threshold,
            max_context_length=budget.max_context_length,
        )

    async def compact_messages(
        self,
        messages: list[dict],
        *,
        model_role: str,
        mode: str,
        tools: list[dict] | None = None,
        token_budget_tracker=None,
        session_id: str | None = None,
    ) -> CompactionResult:
        """Generate a compacted context view.

        `mode` is "passive" or "active" and only affects the boundary preface.

        ``session_id`` enables sticky routing for the compaction call so it can
        read the already-cached conversation prefix for free (no cache_control
        is created here — compaction only reads, never creates a cache entry).
        """
        if not messages:
            raise ValueError("Cannot compact an empty message list")

        # Strip any stale cache_control carried over from a prior turn so the
        # compaction call never creates a cache entry (it only reads). The
        # message dicts are shared with the caller's live list, so build new
        # dicts instead of mutating in place.
        compact_request = [
            {k: v for k, v in m.items() if k != "cache_control"}
            for m in messages
        ]
        compact_request.append({"role": "user", "content": COMPACT_PROMPT})

        summary, compact_usage = await llm_service.call_chat_text(
            messages=compact_request,
            model_role=model_role,
            timeout=300.0,
            max_tokens=self.budget_for_role(model_role).compact_response_max_tokens,
            tools=tools,
            session_id=session_id,
        )

        if token_budget_tracker and compact_usage:
            token_budget_tracker.add_llm_usage(compact_usage)

        if len(summary.strip()) < 50:
            raise ValueError("Compaction summary was too short")

        prefix = ACTIVE_SUMMARY_PREFIX if mode == "active" else PASSIVE_SUMMARY_PREFIX
        boundary_content = f"{prefix}{summary.strip()}"
        compacted = self._build_compacted_messages(messages, boundary_content)
        stats = self.stats_for_messages(compacted, model_role=model_role, tools=tools)
        logger.info(
            "Compacted %s context for role=%s, tokens_after=%s",
            mode, model_role, stats.current_tokens,
        )
        return CompactionResult(
            summary=summary.strip(),
            boundary_content=boundary_content,
            messages=compacted,
            stats=stats,
            usage=compact_usage,
        )

    @staticmethod
    def _build_compacted_messages(messages: list[dict], boundary_content: str) -> list[dict]:
        system_messages = [m for m in messages if m.get("role") == "system"]
        if system_messages:
            return [system_messages[0], {"role": "system", "content": boundary_content}]
        return [{"role": "system", "content": boundary_content}]

    async def stage_session_boundary(
        self,
        uow: UnitOfWork,
        session_id: str,
        boundary_content: str,
    ) -> None:
        await uow.messages.stage_create(
            session_id=session_id,
            role="system",
            content=boundary_content,
            is_boundary=True,
        )

    async def insert_annotation_boundary(
        self,
        uow: UnitOfWork,
        annotation_id: str,
        boundary_content: str,
    ) -> None:
        await uow.messages.stage_boundary_for_annotation(annotation_id, boundary_content)


compaction_service = CompactionService()
