"""
Per-turn token budget tracking for chat and nested agents.

The tracker is intentionally in-memory: it belongs to one running LLM task and
is shared down the agent tree. Persisted accounting still comes from messages.
"""

from dataclasses import dataclass


def format_token_count(n: int) -> str:
    """Format a token count with K/M suffix for human-readable display."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(n)


@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    cached: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output

    def to_dict(self) -> dict:
        return {"input": self.input, "output": self.output, "cached": self.cached}


def extract_llm_usage(usage: dict | None) -> TokenUsage:
    """Extract token usage from a raw litellm response dict.

    Centralizes the litellm-shape (``prompt_tokens`` /
    ``completion_tokens`` / ``prompt_tokens_details.cached_tokens``) →
    internal-shape (``TokenUsage``) translation. Returns a zero
    ``TokenUsage`` when ``usage`` is ``None`` or empty. Always wraps with
    ``int()`` defensively — values may originate from JSON.

    This is the single authoritative unpacker for litellm-shape usage
    dicts; new code should call this instead of re-implementing the
    extraction. For the inverse direction (internal-shape dict →
    underscore-prefixed message keys), see ``LLMLoopRunner.usage_extra``.
    """
    if not usage:
        return TokenUsage(0, 0, 0)
    details = usage.get("prompt_tokens_details") or {}
    return TokenUsage(
        input=int(usage.get("prompt_tokens") or 0),
        output=int(usage.get("completion_tokens") or 0),
        cached=int(details.get("cached_tokens") or 0),
    )


class TokenBudgetExceeded(Exception):
    """Raised when a running turn exceeds the user-provided budget."""

    def __init__(self, usage: TokenUsage, budget: int):
        self.usage = usage
        self.budget = budget
        super().__init__(self.message)

    @property
    def message(self) -> str:
        return (
            "Stopped because this turn exceeded the configured token budget "
            f"({format_token_count(self.usage.total)} used > {format_token_count(self.budget)} budget)."
        )


class TokenBudgetTracker:
    """Shared token budget for one user turn, including nested agent loops."""

    def __init__(self, budget: int | None = None):
        self.budget = int(budget) if budget is not None else None
        self.usage = TokenUsage()

    @property
    def exceeded(self) -> bool:
        return self.budget is not None and self.usage.total > self.budget

    def add_llm_usage(self, usage: dict | None) -> None:
        extracted = extract_llm_usage(usage)
        self.usage.input += extracted.input
        self.usage.output += extracted.output
        self.usage.cached += extracted.cached

    def ensure_within_budget(self) -> None:
        if self.exceeded:
            raise TokenBudgetExceeded(self.usage, self.budget or 0)

    def status_message(self) -> str:
        if self.budget is None:
            return ""
        return (
            f"Token budget exceeded: used {format_token_count(self.usage.total)} tokens "
            f"(input {format_token_count(self.usage.input)}, "
            f"output {format_token_count(self.usage.output)}, "
            f"cached {format_token_count(self.usage.cached)}) "
            f"against a budget of {format_token_count(self.budget)}."
        )
