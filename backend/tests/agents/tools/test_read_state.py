"""Unit tests for read-state coverage merging.

``_merge_coverage`` accumulates the [start, end) line windows the LLM has
seen for one file; the edit tool refuses edits outside this coverage, and
its whole-file shortcut relies on contiguous windows collapsing into a
single interval. ``None`` anywhere means a whole-file read.
"""

from app.agents.tools.read_state import ReadStateCache, _merge_coverage


# ── _merge_coverage — interval algebra ──────────────────────────────

def test_whole_file_read_absorbs_everything():
    assert _merge_coverage(None, (0, 5)) is None
    assert _merge_coverage([(0, 5)], None) is None
    assert _merge_coverage(None, None) is None


def test_disjoint_intervals_stay_separate():
    assert _merge_coverage([(0, 2)], (5, 7)) == [(0, 2), (5, 7)]


def test_overlapping_intervals_collapse():
    assert _merge_coverage([(0, 4)], (2, 6)) == [(0, 6)]


def test_adjacent_intervals_collapse():
    # end == start touches: contiguous coverage must reduce to one interval.
    assert _merge_coverage([(0, 2)], (2, 5)) == [(0, 5)]


def test_contained_interval_is_absorbed():
    assert _merge_coverage([(0, 10)], (3, 4)) == [(0, 10)]


def test_new_window_bridges_existing_intervals():
    # (2, 5) touches both existing intervals: all three reduce to one.
    assert _merge_coverage([(0, 2), (5, 7)], (2, 5)) == [(0, 7)]


def test_unsorted_input_is_sorted_before_merging():
    assert _merge_coverage([(5, 7)], (0, 2)) == [(0, 2), (5, 7)]


# ── accumulation through the cache ──────────────────────────────────

def test_partial_reads_accumulate_at_same_mtime():
    """Successive partial reads of an unchanged file accumulate coverage."""
    cache = ReadStateCache()
    cache.record_read("s1", "a.py", content="x", mtime=1.0,
                      is_partial=True, window=(0, 10))
    cache.record_read("s1", "a.py", content="x", mtime=1.0,
                      is_partial=True, window=(10, 20))

    entry = cache.get("s1", "a.py")
    assert entry.coverage == [(0, 20)]


def test_changed_mtime_discards_stale_coverage():
    """A changed mtime means prior coverage describes stale content: only
    the newest read's window counts."""
    cache = ReadStateCache()
    cache.record_read("s1", "a.py", content="v1", mtime=1.0,
                      is_partial=True, window=(0, 10))
    cache.record_read("s1", "a.py", content="v2", mtime=2.0,
                      is_partial=True, window=(5, 6))

    entry = cache.get("s1", "a.py")
    assert entry.coverage == [(5, 6)]


def test_whole_file_read_after_partial_gives_none():
    cache = ReadStateCache()
    cache.record_read("s1", "a.py", content="x", mtime=1.0,
                      is_partial=True, window=(0, 10))
    cache.record_read("s1", "a.py", content="x", mtime=1.0,
                      is_partial=False, window=None)

    assert cache.get("s1", "a.py").coverage is None
