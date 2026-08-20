"""Session deletion removes the temp storage of every descendant session."""

import pytest

import app.services.ai_service as ai_service_module


class _FakeSessionRepo:
    def __init__(self, descendants):
        self.descendants = descendants
        self.deleted = []

    async def collect_descendant_session_ids(self, session_id):
        return self.descendants[session_id]

    async def delete(self, session_id):
        self.deleted.append(session_id)
        return True


class _FakeTaskStateRepo:
    def __init__(self):
        self.deleted = []

    async def delete_by_session(self, session_id):
        self.deleted.append(session_id)


class _FakeTempService:
    def __init__(self):
        self.deleted = []

    def delete_session_dir(self, project_id, session_id):
        self.deleted.append(session_id)


class _FakeUnitOfWork:
    """Stand-in for UnitOfWork: hands out one fake UoW per context."""

    session_repo = None
    task_state_repo = None

    def __init__(self, project_id):
        self.sessions = self.session_repo
        self.task_state = self.task_state_repo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_session_removes_descendant_temp_dirs(monkeypatch):
    session_repo = _FakeSessionRepo({"s1": ["agent-2", "agent-1", "s1"]})
    task_state_repo = _FakeTaskStateRepo()
    temp = _FakeTempService()
    _FakeUnitOfWork.session_repo = session_repo
    _FakeUnitOfWork.task_state_repo = task_state_repo
    monkeypatch.setattr(ai_service_module, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(ai_service_module, "session_temp_service", temp)

    await ai_service_module.ai_service.delete_session("p1", "s1")

    assert session_repo.deleted == ["s1"]
    assert task_state_repo.deleted == ["s1"]
    # Agent children leave rows via the repo delete; their dirs must go too.
    assert set(temp.deleted) == {"s1", "agent-1", "agent-2"}
