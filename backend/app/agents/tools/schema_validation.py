"""Minimal hand-written JSON-schema validator for tool arguments.

Server-side validation of tool inputs. The LLM's client may enforce its own
schema, but SiGMA never trusts that — without this gate, a malformed tool call
(missing/typo'd argument) can trigger a permission dialog and only fail *after*
the user approves, wasting a round-trip and presenting a broken modal.

``validate_tool_args`` runs in the permission executor *before* the approval
gate. It returns the first error string (suitable to feed straight back to the
LLM) or ``None`` when the args are structurally valid.

Scope: covers every construct the registered tool schemas actually use —
``required``, primitive ``type`` checks, ``enum``, range constraints
(``minimum``/``maximum``/``minLength``/``maxLength``/``minItems``/``maxItems``),
recursion into ``array``/``object`` sub-schemas, and two-branch ``oneOf``. No
``pattern``/``format``/``$ref``/conditional keywords are used by any registered
tool, so they are intentionally unsupported (kept fail-open rather than
mis-reporting).
"""

from __future__ import annotations

from typing import Any, Optional

# JSON-schema "type" keyword -> Python isinstance check. ``integer`` and
# ``number`` are handled separately in _check_type (bool is an int subclass in
# Python but a distinct type in JSON, so it is rejected for both).
_PRIMITIVE_TYPES: dict[str, Any] = {
    "string": str,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_tool_args(input_schema: dict, args: Any) -> Optional[str]:
    """Validate ``args`` against ``input_schema``; return first error or None.

    Non-dict ``args`` are rejected (every tool schema is an object). An empty or
    schema-less ``input_schema`` is treated as permissive — only the constructs
    actually declared are enforced.
    """
    if not isinstance(args, dict):
        return "Error: tool arguments must be a JSON object."
    return _validate_object(input_schema, args, path="args")


def _validate_object(schema: dict, value: dict, *, path: str) -> Optional[str]:
    """Validate an object ``value`` against ``schema`` at ``path``.

    ``path`` is the dotted location used in error messages (e.g.
    ``args.questions[0].options[1].label``) so the LLM can fix the right field.
    """
    props = schema.get("properties") or {}

    for field in schema.get("required", []) or []:
        if field not in value:
            return f"Error: missing required parameter '{field}'."

    for name, raw in value.items():
        # Unknown props are allowed: the loop runner drops them before calling
        # the tool, and rejecting them would produce noisy errors for fields the
        # LLM emits speculatively (e.g. project_id).
        sub_schema = props.get(name)
        if sub_schema is None:
            continue
        err = _validate_value(sub_schema, raw, path=f"{path}.{name}")
        if err:
            return err
    return None


def _validate_value(schema: dict, value: Any, *, path: str) -> Optional[str]:
    """Dispatch a single value through its schema's active keywords."""
    # oneOf: try each branch; valid if any branch passes. Used by 7 tools for
    # string-or-array-of-string fields.
    one_of = schema.get("oneOf")
    if one_of:
        for branch in one_of:
            if _validate_value(branch, value, path=path) is None:
                return None
        return (
            f"Error: parameter at '{path}' does not match any allowed schema."
        )

    declared_type = schema.get("type")
    if declared_type is not None:
        err = _check_type(declared_type, value, path=path)
        if err:
            return err

    err = _check_enum(schema, value, path=path)
    if err:
        return err

    err = _check_range(schema, value, path=path)
    if err:
        return err

    # Recurse into containers.
    if declared_type == "object" and isinstance(value, dict):
        return _validate_object(schema, value, path=path)
    if declared_type == "array" and isinstance(value, list):
        return _validate_array(schema, value, path=path)
    return None


def _check_type(declared_type: str, value: Any, *, path: str) -> Optional[str]:
    """Validate the ``type`` keyword for a single value."""
    if declared_type == "integer":
        # bool is a subclass of int in Python; JSON treats them as distinct
        # types, so reject bare booleans here.
        if isinstance(value, bool) or not isinstance(value, int):
            return f"Error: parameter at '{path}' must be an integer."
        return None
    if declared_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"Error: parameter at '{path}' must be a number."
        return None
    py_type = _PRIMITIVE_TYPES.get(declared_type)
    if py_type is None:
        # Unknown/unsupported type keyword — fail open rather than guessing.
        return None
    # bool is a subclass of int, and str is unrelated; the only ambiguous case
    # is "boolean" where bool matches correctly. No further guard needed.
    if not isinstance(value, py_type):
        return f"Error: parameter at '{path}' must be a {declared_type}."
    return None


def _check_enum(schema: dict, value: Any, *, path: str) -> Optional[str]:
    allowed = schema.get("enum")
    if allowed is None:
        return None
    if value not in allowed:
        return (
            f"Error: parameter at '{path}' must be one of "
            f"{allowed}, got {value!r}."
        )
    return None


def _check_range(schema: dict, value: Any, *, path: str) -> Optional[str]:
    """Validate numeric/length range keywords (minimum/maximum/min*/max*)."""
    if isinstance(value, bool):
        # bool matches "integer" under JSON-schema, but range keywords on a
        # boolean make no sense; skip.
        return None
    if isinstance(value, (int, float)):
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            return f"Error: parameter at '{path}' must be >= {minimum}."
        maximum = schema.get("maximum")
        if maximum is not None and value > maximum:
            return f"Error: parameter at '{path}' must be <= {maximum}."
    if isinstance(value, str):
        min_len = schema.get("minLength")
        if min_len is not None and len(value) < min_len:
            return f"Error: parameter at '{path}' is shorter than {min_len} characters."
        max_len = schema.get("maxLength")
        if max_len is not None and len(value) > max_len:
            return f"Error: parameter at '{path}' is longer than {max_len} characters."
    if isinstance(value, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < min_items:
            return f"Error: parameter at '{path}' must have at least {min_items} item(s)."
        max_items = schema.get("maxItems")
        if max_items is not None and len(value) > max_items:
            return f"Error: parameter at '{path}' must have at most {max_items} item(s)."
    return None


def _validate_array(schema: dict, value: list, *, path: str) -> Optional[str]:
    item_schema = schema.get("items")
    if item_schema is None:
        return None
    for index, item in enumerate(value):
        err = _validate_value(item_schema, item, path=f"{path}[{index}]")
        if err:
            return err
    return None
