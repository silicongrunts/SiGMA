"""
Unit tests for the hand-written tool-argument schema validator.

The validator runs in the permission executor before the approval gate. Its job
is to reject structurally invalid tool calls (missing/typo'd args) so they are
fed back to the LLM instead of surfacing a permission dialog the user would
approve only to see the tool error afterward.

Covers every construct the registered tool schemas actually use: required,
primitive types, enum, range constraints (minimum/maximum/min*/max*), nested
array/object sub-schemas, and two-branch oneOf. ``pattern``/``format``/``$ref``
are intentionally unsupported (no registered tool uses them) and kept fail-open.
"""

import pytest

from app.agents.tools.schema_validation import validate_tool_args


# ── top-level shape ─────────────────────────────────────────────────

def test_non_dict_args_rejected():
    assert validate_tool_args({}, None) == \
        "Error: tool arguments must be a JSON object."
    assert validate_tool_args({}, [1, 2]) == \
        "Error: tool arguments must be a JSON object."


def test_empty_schema_is_permissive():
    # No required, no properties -> nothing to enforce.
    assert validate_tool_args({}, {"anything": 1}) is None


def test_unknown_properties_allowed():
    # The loop runner drops unknown kwargs; rejecting them would be noisy and
    # flag the context-injected params (project_id/session_id).
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "string"}},
    }
    assert validate_tool_args(schema, {"a": "x", "unexpected": 5}) is None


# ── required ────────────────────────────────────────────────────────

def test_missing_required_reported_with_field_name():
    schema = {"required": ["a", "b"], "properties": {"a": {}, "b": {}}}
    assert validate_tool_args(schema, {"a": 1}) == \
        "Error: missing required parameter 'b'."


def test_all_required_present_passes():
    schema = {"required": ["a", "b"], "properties": {"a": {}, "b": {}}}
    assert validate_tool_args(schema, {"a": 1, "b": 2}) is None


# ── primitive types ─────────────────────────────────────────────────

@pytest.mark.parametrize("declared,value,ok", [
    ("string", "hi", True),
    ("string", 5, False),
    ("integer", 5, True),
    ("integer", 5.0, False),   # JSON distinguishes integer from number
    ("integer", True, False),  # bool is not a JSON integer
    ("number", 5, True),
    ("number", 5.5, True),
    ("number", True, False),   # bool is not a JSON number
    ("boolean", True, True),
    ("boolean", 1, False),
    ("boolean", "true", False),
    ("array", [1, 2], True),
    ("array", "ab", False),
    ("object", {"x": 1}, True),
    ("object", [("x", 1)], False),
])
def test_primitive_type_check(declared, value, ok):
    schema = {"properties": {"v": {"type": declared}}}
    result = validate_tool_args(schema, {"v": value})
    if ok:
        assert result is None
    else:
        # Message wording (a/an) is grammar-corrected per type; assert on the
        # stable parts rather than a templated exact string.
        assert result.startswith("Error: parameter at 'args.v' must be")
        assert declared in result


def test_unknown_type_keyword_fails_open():
    # Validator should not pretend to know types it cannot check.
    schema = {"properties": {"v": {"type": "weird"}}}
    assert validate_tool_args(schema, {"v": object()}) is None


# ── enum ────────────────────────────────────────────────────────────

def test_enum_rejects_out_of_set_value():
    schema = {"properties": {"m": {"type": "string", "enum": ["a", "b"]}}}
    result = validate_tool_args(schema, {"m": "c"})
    assert "must be one of" in result
    assert "'c'" in result


def test_enum_accepts_member():
    schema = {"properties": {"m": {"type": "string", "enum": ["a", "b"]}}}
    assert validate_tool_args(schema, {"m": "a"}) is None


# ── range constraints ───────────────────────────────────────────────

def test_numeric_minimum_violation():
    schema = {"properties": {"n": {"type": "integer", "minimum": 0}}}
    assert validate_tool_args(schema, {"n": -1}) == \
        "Error: parameter at 'args.n' must be >= 0."


def test_numeric_maximum_violation():
    schema = {"properties": {"n": {"type": "integer", "maximum": 10}}}
    assert validate_tool_args(schema, {"n": 11}) == \
        "Error: parameter at 'args.n' must be <= 10."


def test_string_length_constraints():
    schema = {"properties": {"s": {
        "type": "string", "minLength": 2, "maxLength": 4,
    }}}
    assert validate_tool_args(schema, {"s": "a"}) is not None
    assert validate_tool_args(schema, {"s": "abcde"}) is not None
    assert validate_tool_args(schema, {"s": "abc"}) is None


def test_array_item_count_constraints():
    schema = {"properties": {"arr": {
        "type": "array", "minItems": 1, "maxItems": 3,
    }}}
    assert validate_tool_args(schema, {"arr": []}) is not None
    assert validate_tool_args(schema, {"arr": [1, 2, 3, 4]}) is not None
    assert validate_tool_args(schema, {"arr": [1]}) is None


# ── nested object / array schemas ───────────────────────────────────

def test_nested_object_required_and_types():
    # Mirrors the ask_user_question shape (3 levels of nesting).
    schema = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["question", "type"],
                    "properties": {
                        "question": {"type": "string"},
                        "type": {"type": "string", "enum": ["single", "multi", "text"]},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["label"],
                                "properties": {"label": {"type": "string"}},
                            },
                        },
                    },
                },
            },
        },
    }
    # Valid
    assert validate_tool_args(schema, {
        "questions": [{"question": "q?", "type": "single",
                       "options": [{"label": "L1"}]}],
    }) is None
    # Missing nested required field
    result = validate_tool_args(schema, {"questions": [{"type": "single"}]})
    assert result == "Error: missing required parameter 'question'."
    # Nested enum error points at the nested path
    result = validate_tool_args(schema, {
        "questions": [{"question": "q?", "type": "bogus"}],
    })
    assert "args.questions[0].type" in result


def test_array_item_type_violation_reports_index():
    schema = {
        "properties": {"todos": {
            "type": "array",
            "items": {"type": "object", "properties": {"c": {"type": "string"}}},
        }},
    }
    result = validate_tool_args(schema, {"todos": [{"c": "ok"}, {"c": 5}]})
    assert "args.todos[1].c" in result


# ── oneOf ───────────────────────────────────────────────────────────

def test_one_of_accepts_either_branch():
    # The string-or-array-of-string pattern used by 7 registered tools.
    schema = {
        "type": "object",
        "required": ["id"],
        "properties": {"id": {"oneOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ]}},
    }
    assert validate_tool_args(schema, {"id": "abc"}) is None
    assert validate_tool_args(schema, {"id": ["a", "b"]}) is None


def test_one_of_rejects_value_matching_no_branch():
    schema = {
        "properties": {"id": {"oneOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ]}},
    }
    assert validate_tool_args(schema, {"id": 5}) == \
        "Error: parameter at 'args.id' does not match any allowed schema."
    # array of non-strings matches neither branch
    assert validate_tool_args(schema, {"id": [1, 2]}) == \
        "Error: parameter at 'args.id' does not match any allowed schema."


# ── error format consistency ────────────────────────────────────────

def test_error_strings_are_prefixed_with_error():
    """Preflight/tool errors use 'Error:' prefix; the validator must match so
    the LLM sees one consistent failure shape."""
    schema = {"required": ["x"], "properties": {"x": {"type": "string"}}}
    assert validate_tool_args(schema, {}).startswith("Error:")
    assert validate_tool_args(schema, {"x": 1}).startswith("Error:")
