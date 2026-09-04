"""Chat search: matching over user-visible UI entries and snippet building."""

from app.core.chat_search import (
    SNIPPET_CONTEXT,
    build_snippet,
    compile_search_pattern,
    sql_prefilter_reliable,
    visible_search_matches,
)


def _entry(mid, role, content, seq=0):
    return {"id": mid, "role": role, "content": content, "seq": seq, "created_at": None}


def test_matches_user_and_assistant_bubbles_case_insensitively():
    entries = [
        _entry("m0", "user", "What is the retrieval pipeline?", seq=0),
        _entry("m1", "SiGMA", "The RETRIEVAL pipeline reranks chunks.", seq=1),
    ]
    matches = visible_search_matches(entries, compile_search_pattern("retrieval"))
    assert [m["id"] for m in matches] == ["m0", "m1"]
    assert [m["role"] for m in matches] == ["user", "SiGMA"]
    assert all("retrieval" in m["snippet"].lower() for m in matches)


def test_cjk_single_character_substring_matches():
    entries = [_entry("m0", "user", "请帮我检索文献")]
    matches = visible_search_matches(entries, compile_search_pattern("检索"))
    assert len(matches) == 1
    assert "检索" in matches[0]["snippet"]


def test_system_boundary_and_tool_roles_never_match():
    entries = [
        _entry("m0", "system", "earlier summary mentioning needle"),
        _entry("m1", "SiGMA", "unrelated answer"),
    ]
    assert visible_search_matches(entries, compile_search_pattern("needle")) == []


def test_regex_special_characters_in_query_match_literally():
    entries = [_entry("m0", "user", "a.b*c lookup")]
    matches = visible_search_matches(entries, compile_search_pattern("a.b*"))
    assert len(matches) == 1


def test_sql_prefilter_reliable_classifies_needles():
    # ASCII letters fold in SQL; caseless scripts and punctuation never vary
    # in case, so both are safe. Non-ASCII cased letters are not folded by
    # SQLite and can occur uppercase in stored rows — those must scan in Python.
    assert sql_prefilter_reliable("retrieval pipeline v2 (beta)?")
    assert sql_prefilter_reliable("请帮我检索文献")
    assert not sql_prefilter_reliable("école")
    assert not sql_prefilter_reliable("Привет мир")


def test_snippet_collapses_whitespace_and_marks_truncation():
    content = "x" * 200 + " needle " + "y" * 200
    snippet = build_snippet(content, 200, 207)
    assert snippet.startswith("...")
    assert snippet.endswith("...")
    assert "needle" in snippet
    assert len(snippet) <= 2 * SNIPPET_CONTEXT + len(" needle ") + 6


def test_snippet_short_content_has_no_ellipsis():
    snippet = build_snippet("short needle tail", 6, 12)
    assert snippet == "short needle tail"


def test_snippet_collapses_newlines_to_single_line():
    snippet = build_snippet("line one\nline two\tneedle", 18, 24)
    assert "\n" not in snippet and "\t" not in snippet
    assert "line one line two needle" in snippet
