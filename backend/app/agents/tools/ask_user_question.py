"""
ask_user_question tool — pause the loop and ask the user questions.

When the LLM calls this tool, the QueryLoop saves a checkpoint, sends an
awaiting_input SSE event, and exits. The frontend shows a modal with the
questions. The user's answers are fed back via a new chat request with
interaction_response, and the loop resumes.
"""

import json

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.agents.prompts import PROMPT_ASK_USER_QUESTION


async def _ask_user_question(questions, answers=None, **_kwargs):
    """Phase 1 (no answers): return interaction data for frontend.
    Phase 2 (has answers): format user's answers as tool result.
    """
    # Robust parsing: LLM may send questions as a JSON string
    if isinstance(questions, str):
        try:
            questions = json.loads(questions)
        except (json.JSONDecodeError, TypeError):
            questions = []
    if answers is not None:
        return _format_answers(answers)

    # Phase 1: validate before surfacing to the user. An invalid payload is
    # returned as an error string; the runner feeds it back to the LLM as a
    # tool result and does not open a modal.
    err = validate_questions(questions)
    if err:
        return err
    return {
        "interaction_type": "ask_user_question",
        "questions": questions,
    }


def validate_questions(questions) -> str | None:
    """Validate the ask_user_question payload.

    Returns a descriptive error message on failure, or None on success.
    Enforces constraints the JSON schema can't express: each question text
    and each option label/description must be non-empty (after trim), option
    labels must be unique within a question, and single/multi questions need
    2-6 options.
    """
    if not isinstance(questions, list) or not questions:
        return "Error: 'questions' must be a non-empty array."
    if len(questions) > 4:
        return f"Error: too many questions ({len(questions)}); maximum is 4."

    for qi, q in enumerate(questions, 1):
        if not isinstance(q, dict):
            return f"Error: question {qi} must be an object."

        qtype = q.get("type")
        if qtype not in ("single", "multi", "text"):
            return (
                f"Error: question {qi} has invalid or missing 'type' "
                f"({qtype!r}); must be 'single', 'multi', or 'text'."
            )

        qtext = (q.get("question") or "").strip()
        if not qtext:
            return (
                f"Error: question {qi} has an empty 'question'; "
                "provide the question text."
            )

        if qtype in ("single", "multi"):
            options = q.get("options")
            if not isinstance(options, list) or len(options) < 2:
                return f"Error: question {qi} ({qtype}) needs at least 2 options."
            if len(options) > 6:
                return (
                    f"Error: question {qi} ({qtype}) has {len(options)} "
                    "options; maximum is 6."
                )
            labels: list = []
            for oi, opt in enumerate(options, 1):
                if not isinstance(opt, dict):
                    return f"Error: question {qi} option {oi} must be an object."
                label = (opt.get("label") or "").strip()
                if not label:
                    return (
                        f"Error: question {qi} option {oi} has an empty "
                        "'label'; provide a label."
                    )
                desc = (opt.get("description") or "").strip()
                if not desc:
                    return (
                        f"Error: question {qi} option {oi} has an empty "
                        "'description'."
                    )
                if label in labels:
                    return (
                        f"Error: question {qi} has duplicate option label "
                        f"{label!r}; each option label must be unique within "
                        "a question."
                    )
                labels.append(label)
    return None


def _truncate(text: str, n: int = 80) -> str:
    """Trim text to n characters with an ellipsis."""
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + "..."


def _format_answers(answers) -> str:
    """Format the user's answers as a readable tool result.

    `answers` is always a list of {question, answer} objects from the frontend.
    `answer` is a string (single/text) or a list of strings (multi).
    """
    if not isinstance(answers, list) or not answers:
        return "User provided no answers."

    pairs = []
    for i, item in enumerate(answers, 1):
        question = item.get("question", "")
        answer = item.get("answer", "(no answer)")
        if isinstance(answer, list):
            answer = ", ".join(answer) if answer else "(no answer)"
        pairs.append(f"Question {i}: {_truncate(question)}\nUser Answer: {answer}")

    return "\n\n".join(pairs)


tool_registry.register(ToolDefinition(
    name="ask_user_question",
    description=(
        "Ask the user a question (single-choice, multi-choice, or open-ended "
        "text) to gather information or make decisions."
    ),
    prompt=PROMPT_ASK_USER_QUESTION,
    input_schema={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The complete question to ask the user",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["single", "multi", "text"],
                            "description": "single = choose one option; multi = choose several; text = free-form answer",
                        },
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 6,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Display text (1-5 words)",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "What this option means (one line)",
                                    },
                                    "recommended": {
                                        "type": "boolean",
                                        "description": "Mark at most one option per question as recommended",
                                        "default": False,
                                    },
                                },
                                "required": ["label", "description"],
                            },
                            "description": "Available choices (2-6). Required for single/multi; omit for text.",
                        },
                    },
                    "required": ["question", "type"],
                },
            },
        },
        "required": ["questions"],
    },
    call=lambda questions, **kwargs: _ask_user_question(questions, **kwargs),
    requires_user_interaction=True,
    is_read_only=True,
))
