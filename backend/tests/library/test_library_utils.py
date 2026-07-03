"""
Tests for library_tools pure helpers — ID parsing, content formatting,
and search result rendering.
"""

import pytest

from app.agents.tools.library_tools import (
    short_id,
    parse_ids,
    parse_fields,
    content_preview,
    format_search_results,
    format_directory_listing,
    format_document_content,
)


# ---------------------------------------------------------------------------
# short_id
# ---------------------------------------------------------------------------

def test_short_id_truncates():
    assert short_id("abcdef1234567890") == "abcdef12"


def test_short_id_short_string():
    assert short_id("abc") == "abc"


def test_short_id_empty():
    assert short_id("") == ""


# ---------------------------------------------------------------------------
# parse_ids
# ---------------------------------------------------------------------------

def test_parse_ids_list():
    assert parse_ids(["a", "b", "c"]) == ["a", "b", "c"]


def test_parse_ids_list_strips():
    assert parse_ids([" a ", " b"]) == ["a", "b"]


def test_parse_ids_json_string():
    assert parse_ids('["a","b"]') == ["a", "b"]


def test_parse_ids_comma_string():
    assert parse_ids("a,b,c") == ["a", "b", "c"]


def test_parse_ids_single_string():
    assert parse_ids("abc") == ["abc"]


def test_parse_ids_empty_string():
    assert parse_ids("") == []


def test_parse_ids_none():
    assert parse_ids(None) == []


def test_parse_ids_invalid_json():
    assert parse_ids("[invalid") == ["[invalid"]


# ---------------------------------------------------------------------------
# parse_fields
# ---------------------------------------------------------------------------

def test_parse_fields_default_empty():
    assert parse_fields("") == ["content"]


def test_parse_fields_default_content():
    assert parse_fields("content") == ["content"]


def test_parse_fields_none():
    assert parse_fields(None) == ["content"]


def test_parse_fields_list():
    assert parse_fields(["keywords", "content"]) == ["keywords", "content"]


def test_parse_fields_json_string():
    assert parse_fields('["keywords","description"]') == ["keywords", "description"]


def test_parse_fields_comma_string():
    assert parse_fields("keywords,description") == ["keywords", "description"]


def test_parse_fields_single():
    assert parse_fields("description") == ["description"]


def test_parse_fields_empty_list():
    assert parse_fields([]) == ["content"]


# ---------------------------------------------------------------------------
# content_preview
# ---------------------------------------------------------------------------

def test_content_preview_short():
    text = "short text"
    assert content_preview(text) == text


def test_content_preview_long():
    text = "x" * 500
    result = content_preview(text, max_len=50)
    assert "..." in result
    assert len(result) < len(text)


def test_content_preview_exact_threshold():
    text = "x" * 150  # 50 * 3
    assert content_preview(text) == text


# ---------------------------------------------------------------------------
# format_directory_listing
# ---------------------------------------------------------------------------

def test_format_directory_listing_empty():
    assert format_directory_listing([], "papers") == 'Directory "papers": (empty)'


def test_format_directory_listing_with_items():
    docs = [
        {"id": "abcdef12-0000-0000-0000-000000000000", "title": "My Paper",
         "keywords": ["ml", "nlp"]},
        {"id": "12345678-0000-0000-0000-000000000000", "title": "Folder",
         "is_folder": True},
    ]
    result = format_directory_listing(docs, "papers")
    assert "[abcdef12]" in result
    assert "[12345678]" in result
    assert "My Paper" in result
    assert "Folder/" in result
    assert "ml, nlp" in result


def test_format_directory_listing_no_keywords():
    docs = [
        {"id": "abcdef12-0000-0000-0000-000000000000", "title": "No KWs"},
    ]
    result = format_directory_listing(docs, "papers")
    assert "No KWs" in result
    assert " — " not in result


# ---------------------------------------------------------------------------
# format_document_content
# ---------------------------------------------------------------------------

def _mock_doc(content="hello\nworld", keywords=None, description=None,
              processing_status="completed"):
    """Create a mock document object."""
    import json as _json
    doc = type("Doc", (), {})()
    doc.content = content
    doc.keywords = _json.dumps(keywords or [])
    doc.description = description
    doc.processing_status = processing_status
    return doc


def test_format_document_content_basic():
    doc = _mock_doc("line1\nline2\nline3")
    result = format_document_content(doc, ["content"])
    assert "1\tline1" in result
    assert "2\tline2" in result
    assert "3\tline3" in result


def test_format_document_content_with_offset():
    doc = _mock_doc("line1\nline2\nline3\nline4\nline5")
    result = format_document_content(doc, ["content"], offset=2, limit=2)
    assert "3\tline3" in result
    assert "4\tline4" in result
    assert "skipped first 2 lines" in result


def test_format_document_content_with_limit():
    doc = _mock_doc("a\nb\nc\nd\ne")
    result = format_document_content(doc, ["content"], offset=0, limit=2)
    assert "1\ta" in result
    assert "2\tb" in result
    assert "more lines not shown" in result


def test_format_document_content_keywords():
    doc = _mock_doc("", keywords=["ai", "nlp"])
    result = format_document_content(doc, ["keywords"])
    assert "ai, nlp" in result


def test_format_document_content_description():
    doc = _mock_doc("", description="A great paper")
    result = format_document_content(doc, ["description"])
    assert "A great paper" in result


def test_format_document_content_pending_status():
    doc = _mock_doc("text", processing_status="processing")
    result = format_document_content(doc, ["content"])
    assert "processing" in result


def test_format_document_content_multiple_fields():
    doc = _mock_doc("hello", keywords=["test"], description="desc")
    result = format_document_content(doc, ["keywords", "description", "content"])
    assert "test" in result
    assert "desc" in result
    assert "1\thello" in result


# ---------------------------------------------------------------------------
# format_search_results — XML escaping
# ---------------------------------------------------------------------------

def _make_entry(title="Test", description="Desc", keywords=None,
                matches=None):
    if matches is None:
        matches = [{"text": "matched", "line": 1, "score": 0.9}]
    return {
        "id": "abcdef12-0000-0000-0000-000000000000",
        "title": title,
        "description": description,
        "keywords": keywords or [],
        "matches": matches,
    }


def test_format_search_results_basic():
    entries = [_make_entry()]
    result = format_search_results(entries, total=1, start=1, end=1)
    assert "<doc>" in result
    assert "<title>Test</title>" in result
    assert "<description>Desc</description>" in result
    assert 'score="0.9"' in result
    assert 'line="1"' in result
    assert ">matched<" in result


def test_format_search_results_escapes_special_chars():
    """Title/description/match with <, & must be escaped."""
    entries = [_make_entry(
        title='A <B> & C "D"',
        description='x < y & z',
        matches=[{"text": 'foo </doc> bar', "line": 5, "score": 0.5}],
    )]
    result = format_search_results(entries, total=1, start=1, end=1)
    # Raw < > & should NOT appear unescaped inside XML tags
    assert "&lt;B&gt;" in result
    assert "&amp;" in result
    assert "&lt;/doc&gt;" in result
    # The raw chars should not appear in tag content
    assert "<B>" not in result
    assert "</doc> bar" not in result


def test_format_search_results_escapes_keywords():
    entries = [_make_entry(keywords=["a <b>", "c&d"])]
    result = format_search_results(entries, total=1, start=1, end=1)
    assert "&lt;b&gt;" in result
    assert "c&amp;d" in result


def test_format_search_results_header_shows_window_when_paginated():
    """When start/end are provided, header shows the page window."""
    entries = [_make_entry(), _make_entry()]
    result = format_search_results(entries, total=250, start=51, end=52)
    assert "Found 250 result(s)" in result
    assert "showing 51-52" in result
    # No "browsable" cap wording anymore
    assert "browsable" not in result


def test_format_search_results_header_omits_window_for_semantic():
    """When start/end are None (semantic mode), header is a plain count."""
    entries = [_make_entry()]
    result = format_search_results(entries, total=5, start=None, end=None)
    assert "Found 5 result(s):" in result
    assert "showing" not in result


def test_format_search_results_marks_description_truncation():
    """Descriptions longer than 300 chars get truncated with a visible marker."""
    long_description = "x" * 400
    entries = [_make_entry(description=long_description)]
    result = format_search_results(entries, total=1, start=1, end=1)
    assert "..." in result
    # Truncated to 300 + marker, not the full 400
    assert "x" * 400 not in result


def test_format_search_results_marks_match_text_truncation():
    """Match text longer than 400 chars get truncated with a visible marker."""
    long_match = "y" * 500
    entries = [_make_entry(matches=[{"text": long_match, "line": 1}])]
    result = format_search_results(entries, total=1, start=1, end=1)
    assert "..." in result
    assert "y" * 500 not in result


def test_format_search_results_no_marker_when_under_threshold():
    """Short descriptions and match text should NOT get a marker."""
    entries = [_make_entry(description="short", matches=[{"text": "tiny", "line": 1}])]
    result = format_search_results(entries, total=1, start=1, end=1)
    # Both fields short enough — no ellipsis anywhere in the entry body
    assert "<description>short</description>" in result
    assert ">tiny<" in result
