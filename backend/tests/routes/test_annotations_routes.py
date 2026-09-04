from types import SimpleNamespace

import pytest

from app.core.exceptions import ProjectNotFoundError, ValidationError
from app.models.requests import AnnotationReplyRequest, CreateAnnotationRequest
from app.routes import annotations


@pytest.mark.route
@pytest.mark.asyncio
async def test_create_annotation_rejects_mismatched_file_path(monkeypatch, project_gate):
    calls = []
    project_gate(monkeypatch, annotations, calls)

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


@pytest.mark.route
@pytest.mark.asyncio
async def test_cancel_annotation_reply_validates_project_before_cancel(monkeypatch, project_gate):
    calls = []
    project_gate(monkeypatch, annotations, calls)

    async def cancel_task(project_id, task_id):
        calls.append(("cancel", project_id, task_id))
        return {"cancelled": True, "status": "cancelling", "task_id": task_id}

    monkeypatch.setattr(annotations, "ai_service", SimpleNamespace(cancel_task=cancel_task))

    result = await annotations.cancel_annotation_reply("project-1", "task-1")

    assert result["data"] == {
        "cancelled": True,
        "status": "cancelling",
        "task_id": "task-1",
    }
    # Project validation precedes the cancel call.
    assert calls == [("project", "project-1"), ("cancel", "project-1", "task-1")]


# ---------------------------------------------------------------------------
# Error translation (HTTP level): service exceptions must reach the client as
# the exception's status code with the unified error envelope
# {"request_id", "success", "error", "data"}.
# ---------------------------------------------------------------------------

@pytest.mark.route
@pytest.mark.asyncio
async def test_path_mismatch_translates_to_422_error_envelope(client, no_password, monkeypatch, project_gate):
    calls = []
    project_gate(monkeypatch, annotations, calls)

    r = await client.post(
        "/api/v1/annotations/project-1/create",
        params={"path": "paper.tex"},
        json={"filePath": "other.tex", "from": 0, "to": 1},
    )

    assert r.status_code == 422
    body = r.json()
    assert set(body) == {"request_id", "success", "error", "data"}
    assert body["success"] is False
    assert body["error"] == "Annotation file path does not match the request path"
    assert body["data"] is None


@pytest.mark.route
@pytest.mark.asyncio
async def test_unknown_project_translates_to_404_error_envelope(client, no_password, monkeypatch):
    def get_project_path(project_id):
        raise ProjectNotFoundError(project_id)

    monkeypatch.setattr(
        annotations,
        "project_service",
        SimpleNamespace(get_project_path=get_project_path),
    )

    r = await client.delete("/api/v1/annotations/project-1/anno-1")

    assert r.status_code == 404
    body = r.json()
    assert set(body) == {"request_id", "success", "error", "data"}
    assert body["success"] is False
    assert body["error"] == "Project not found: project-1"
    assert body["data"] is None
