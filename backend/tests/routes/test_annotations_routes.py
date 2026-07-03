from types import SimpleNamespace

import pytest

from app.core.exceptions import ValidationError
from app.models.requests import AnnotationReplyRequest, CreateAnnotationRequest
from app.routes import annotations


@pytest.mark.route
@pytest.mark.asyncio
async def test_create_annotation_rejects_mismatched_file_path(monkeypatch):
    monkeypatch.setattr(
        annotations,
        "project_service",
        SimpleNamespace(get_project_path=lambda project_id: "/project"),
    )

    with pytest.raises(ValidationError):
        await annotations.create_annotation(
            "project-1",
            path="paper.tex",
            data=CreateAnnotationRequest(
                filePath="other.tex",
                **{"from": 0, "to": 1},
            ),
        )


@pytest.mark.route
@pytest.mark.asyncio
async def test_reply_annotation_appends_user_message(monkeypatch):
    calls = {}

    async def reply_annotation(**kwargs):
        calls.update(kwargs)
        return {"id": "reply-1"}

    monkeypatch.setattr(
        annotations,
        "annotation_service",
        SimpleNamespace(reply_annotation=reply_annotation),
    )

    result = await annotations.reply_annotation(
        "project-1",
        AnnotationReplyRequest(annotationId="anno-1", content="answer"),
    )

    assert result["success"] is True
    assert calls == {
        "project_id": "project-1",
        "file_path": "",
        "anno_id": "anno-1",
        "content": "answer",
        "role": "user",
    }
