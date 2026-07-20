"""Shared formatting helpers for tool outputs.

These helpers standardize how paginated/truncated tool results communicate
their position and remaining content to the LLM, so that read, library_get,
and grep (content mode) describe truncation in the same shape.
"""


def format_range_footer(
    shown_start: int, shown_end: int, total: int, *, unit: str = "lines",
) -> str:
    """Return a standardized pagination footer string.

    Describes the visible 1-indexed window ``shown_start..shown_end`` out of
    ``total`` items, plus how many remain hidden. Returns an empty string when
    nothing is hidden (``shown_end >= total``), so callers can append
    unconditionally.

    ``unit`` controls the noun in the footer (``lines``/``results``/``matches``)
    so different tools can share the same shape without forcing an inaccurate
    word. Example output::

        Showing lines 1-200 of 543 (343 more not shown)
    """
    if shown_end >= total:
        return ""
    hidden = total - shown_end
    return (
        f"\n\nShowing {unit} {shown_start}-{shown_end} of {total} "
        f"({hidden} more not shown)"
    )
