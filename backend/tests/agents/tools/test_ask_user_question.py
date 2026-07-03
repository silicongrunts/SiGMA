"""
Unit tests for ask_user_question validation and answer formatting.
"""
from app.agents.tools.ask_user_question import validate_questions, _format_answers


def _single(**over):
    base = {
        "question": "Which?",
        "type": "single",
        "options": [
            {"label": "A", "description": "a"},
            {"label": "B", "description": "b"},
        ],
    }
    base.update(over)
    return base


# ── validate_questions: valid cases ──────────────────────────────────

def test_valid_single_passes():
    assert validate_questions([_single()]) is None


def test_valid_multi_passes():
    assert validate_questions([_single(type="multi")]) is None


def test_valid_text_passes():
    assert validate_questions([{"question": "Name?", "type": "text"}]) is None


def test_valid_text_ignores_options():
    # text type does not require options; their absence is fine
    assert validate_questions([{"question": "x", "type": "text"}]) is None


def test_valid_max_questions_passes():
    assert validate_questions([_single()] * 4) is None


def test_valid_six_options_passes():
    opts = [{"label": f"L{i}", "description": "d"} for i in range(6)]
    assert validate_questions([_single(options=opts)]) is None


# ── validate_questions: rejection cases ──────────────────────────────

def test_empty_questions_rejected():
    assert "non-empty array" in validate_questions([])
    assert "non-empty array" in validate_questions(None)


def test_not_a_list_rejected():
    assert "non-empty array" in validate_questions({"question": "x"})


def test_too_many_questions_rejected():
    assert "maximum is 4" in validate_questions([_single()] * 5)


def test_missing_type_rejected():
    err = validate_questions([{"question": "x", "options": [
        {"label": "A", "description": "a"},
        {"label": "B", "description": "b"}]}])
    assert err and "'type'" in err


def test_invalid_type_rejected():
    err = validate_questions([{"question": "x", "type": "choice"}])
    assert err and "'type'" in err


def test_empty_question_text_rejected():
    err = validate_questions([{"question": "   ", "type": "text"}])
    assert err and "empty 'question'" in err


def test_empty_label_rejected():
    err = validate_questions([_single(options=[
        {"label": "", "description": "a"},
        {"label": "B", "description": "b"}])])
    assert err and "empty 'label'" in err


def test_whitespace_label_rejected():
    err = validate_questions([_single(options=[
        {"label": "   ", "description": "a"},
        {"label": "B", "description": "b"}])])
    assert err and "empty 'label'" in err


def test_duplicate_label_rejected():
    err = validate_questions([_single(options=[
        {"label": "X", "description": "a"},
        {"label": "X", "description": "b"}])])
    assert err and "duplicate" in err and "X" in err


def test_empty_description_rejected():
    err = validate_questions([_single(options=[
        {"label": "A", "description": "  "},
        {"label": "B", "description": "b"}])])
    assert err and "empty 'description'" in err


def test_too_few_options_rejected():
    err = validate_questions([_single(options=[
        {"label": "A", "description": "a"}])])
    assert err and "at least 2 options" in err


def test_too_many_options_rejected():
    opts = [{"label": f"L{i}", "description": "d"} for i in range(7)]
    err = validate_questions([_single(options=opts)])
    assert err and "maximum is 6" in err


def test_error_pinpoints_question_index():
    # Second question is bad — error should reference question 2
    err = validate_questions([_single(), {"question": "", "type": "text"}])
    assert err and "question 2" in err


# ── _format_answers ──────────────────────────────────────────────────

def test_format_answers_single_and_multi():
    out = _format_answers([
        {"question": "DB?", "answer": "Postgres"},
        {"question": "Cache?", "answer": ["Redis", "LRU"]},
    ])
    assert "Question 1: DB?" in out
    assert "User Answer: Postgres" in out
    assert "Question 2: Cache?" in out
    assert "User Answer: Redis, LRU" in out


def test_format_answers_empty():
    assert _format_answers(None) == "User provided no answers."
    assert _format_answers([]) == "User provided no answers."


def test_format_answers_truncates_long_question():
    long_q = "x" * 200
    out = _format_answers([{"question": long_q, "answer": "A"}])
    assert "..." in out
    assert long_q not in out  # full text truncated
