"""Unit tests for the per-session ReadStateCache."""

from app.agents.tools.read_state import ReadStateCache, ReadStateEntry


def test_partial_read_counts_as_read():
    cache = ReadStateCache()
    cache.record_read("s1", "foo.py", content="x", mtime=1.0, is_partial=True)
    assert cache.get("s1", "foo.py") is not None


def test_get_returns_entry():
    cache = ReadStateCache()
    cache.record_read("s1", "foo.py", content="hello", mtime=2.5, is_partial=False)
    entry = cache.get("s1", "foo.py")
    assert isinstance(entry, ReadStateEntry)
    assert entry.content == "hello"
    assert entry.mtime == 2.5
    assert entry.is_partial is False


def test_clear_removes_all_entries_for_session():
    cache = ReadStateCache()
    cache.record_read("s1", "a.py", content="x", mtime=1.0, is_partial=False)
    cache.record_read("s1", "b.py", content="y", mtime=1.0, is_partial=False)
    cache.record_read("s2", "c.py", content="z", mtime=1.0, is_partial=False)

    cache.clear("s1")
    assert cache.get("s1", "a.py") is None
    assert cache.get("s1", "b.py") is None
    # Other sessions are untouched
    assert cache.get("s2", "c.py") is not None


def test_clear_missing_session_is_noop():
    cache = ReadStateCache()
    cache.clear("never-existed")  # should not raise


def test_sessions_are_isolated():
    cache = ReadStateCache()
    cache.record_read("s1", "foo.py", content="x", mtime=1.0, is_partial=False)
    assert cache.get("s2", "foo.py") is None


def test_record_read_overwrites_prior_entry():
    cache = ReadStateCache()
    cache.record_read("s1", "foo.py", content="v1", mtime=1.0, is_partial=True)
    cache.record_read("s1", "foo.py", content="v2", mtime=2.0, is_partial=False)
    entry = cache.get("s1", "foo.py")
    assert entry.content == "v2"
    assert entry.mtime == 2.0
    assert entry.is_partial is False
