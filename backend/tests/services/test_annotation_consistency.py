import asyncio
import json

import pytest

from app.core.utils import generate_id
from app.database.unit_of_work import UnitOfWork
from app.database.repos.annotation_repo import AnnotationRepository
from app.services.annotation_service import annotation_service
from app.services.file_service import file_service
from app.services.project_service import project_service
from app.core.config import settings
from app.core.exceptions import AnnotationConflictError, TaskActiveError


@pytest.fixture(autouse=True)
def use_fixture_project_root(project, monkeypatch):
    root = settings.USERDATA_DIR.resolve()
    monkeypatch.setattr(project_service, "USERDATA_DIR", root)
    monkeypatch.setattr(project_service, "SIGMA_DIR", root / ".SiGMA")
    monkeypatch.setattr(project_service, "PROJECTS_FILE", root / ".SiGMA" / "projects.json")


@pytest.mark.asyncio
async def test_empty_file_save_and_explicit_delete_survive_reload(project):
    path = "paper.md"
    full_path = settings.get_project_path(project) / path
    full_path.write_text("hello world")

    loaded = await annotation_service.get_annotations(project, path)
    saved = await annotation_service.save_document(
        project, path, "hello world!", loaded["fileHash"], loaded["revision"], [], []
    )
    assert saved["success"]
    assert full_path.read_text() == "hello world!"

    created = await annotation_service.add_annotation(project, path, 0, 5, "note")
    current = await annotation_service.get_annotations(project, path)
    deleted = await annotation_service.save_annotations(
        project, path, [], current["revision"], current["fileHash"], [created["id"]]
    )
    assert deleted["success"]
    refreshed = await annotation_service.get_annotations(project, path)
    assert refreshed["annotations"] == []


@pytest.mark.asyncio
async def test_two_sessions_only_one_annotation_cas_writer_succeeds(project):
    path = "paper.md"
    (settings.get_project_path(project) / path).write_text("hello world")
    created = await annotation_service.add_annotation(project, path, 0, 5, "note")
    loaded = await annotation_service.get_annotations(project, path)
    update = {"id": created["id"], "from": 0, "to": 5, "originalText": "hello"}

    async def write():
        return await annotation_service.save_annotations(
            project, path, [update], loaded["revision"], loaded["fileHash"], []
        )

    results = await asyncio.gather(write(), write(), return_exceptions=True)
    assert sum(isinstance(result, dict) and result["success"] for result in results) == 1
    assert sum(getattr(result, "status_code", None) == 409 for result in results) == 1


@pytest.mark.asyncio
async def test_external_file_change_reconciles_published_snapshot(project):
    path = "paper.md"
    full_path = settings.get_project_path(project) / path
    full_path.write_text("hello world")
    created = await annotation_service.add_annotation(project, path, 0, 5, "note")
    full_path.write_text("changed world")

    loaded = await annotation_service.get_annotations(project, path)
    assert [item["id"] for item in loaded["annotations"]] == [created["id"]]
    result = await annotation_service.save_annotations(
        project, path, [{"id": created["id"], "from": 0, "to": 7, "originalText": "changed"}],
        loaded["revision"], loaded["fileHash"], [],
    )
    assert result["success"]


@pytest.mark.asyncio
async def test_recovery_does_not_replace_unrelated_disk_version(project):
    path = "paper.md"
    full_path = settings.get_project_path(project) / path
    full_path.write_text("old")
    loaded = await annotation_service.get_annotations(project, path)
    new_content = "new"
    old_hash = file_service.compute_hash("old")
    new_hash = file_service.compute_hash(new_content)
    async with UnitOfWork(project) as uow:
        await uow.annotations.create_transaction(
            id=generate_id(), file_path=path,
            expected_revision=loaded["revision"], expected_file_hash=old_hash,
            new_file_hash=new_hash, old_content="old",
            mutations=json.dumps({"upserts": [], "deleteIds": []}),
        )
    full_path.write_text("someone else")
    await annotation_service.recover_transactions(project)
    assert full_path.read_text() == "someone else"
    async with UnitOfWork(project) as uow:
        assert await uow.annotations.get_transactions() == []

    loaded = await annotation_service.get_annotations(project, path)
    assert loaded["fileHash"] == file_service.compute_hash("someone else")
    saved = await annotation_service.save_document(
        project, path, "someone else!", loaded["fileHash"], loaded["revision"], [], [],
    )
    assert saved["success"]

@pytest.mark.asyncio
async def test_legacy_annotation_is_preserved_when_state_is_first_created(project):
    path = "legacy.md"
    full_path = settings.get_project_path(project) / path
    full_path.write_text("legacy text")
    annotation_id = generate_id()
    async with UnitOfWork(project) as uow:
        await uow.annotations.apply_mutation_cas(
            path,
            [{
                "id": annotation_id,
                "from": 0,
                "to": 6,
                "originalText": "legacy",
            }],
            [],
            0,
            file_service.compute_hash("legacy text"),
        )
    loaded = await annotation_service.get_annotations(project, path)
    assert [item["id"] for item in loaded["annotations"]] == [annotation_id]
    assert loaded["revision"] == 1


@pytest.mark.asyncio
async def test_independent_sessions_reject_stale_create_update_delete(project):
    path = "paper.md"
    settings.get_project_path(project).joinpath(path).write_text("hello world")
    loaded = await annotation_service.get_annotations(project, path)
    stale_revision = loaded["revision"]
    stale_hash = loaded["fileHash"]
    first_id = generate_id()
    second_id = generate_id()
    mutation = {"id": first_id, "from": 0, "to": 5, "originalText": "hello"}

    async with UnitOfWork(project, immediate=True) as first:
        await first.annotations.apply_mutation_cas(path, [mutation], [], stale_revision, stale_hash)
    async with UnitOfWork(project, immediate=True) as second:
        with pytest.raises(ValueError):
            await second.annotations.apply_mutation_cas(
                path,
                [{**mutation, "id": second_id}],
                [],
                stale_revision,
                stale_hash,
            )

    loaded = await annotation_service.get_annotations(project, path)
    mutation["from"] = 1
    async with UnitOfWork(project, immediate=True) as first:
        await first.annotations.apply_mutation_cas(
            path, [mutation], [], loaded["revision"], loaded["fileHash"],
        )
    async with UnitOfWork(project, immediate=True) as second:
        with pytest.raises(ValueError):
            await second.annotations.apply_mutation_cas(
                path, [mutation], [], loaded["revision"], loaded["fileHash"],
            )

    loaded = await annotation_service.get_annotations(project, path)
    async with UnitOfWork(project, immediate=True) as first:
        await first.annotations.apply_mutation_cas(
            path, [], [first_id], loaded["revision"], loaded["fileHash"],
        )
    async with UnitOfWork(project, immediate=True) as second:
        with pytest.raises(ValueError):
            await second.annotations.apply_mutation_cas(
                path, [], [first_id], loaded["revision"], loaded["fileHash"],
            )


@pytest.mark.asyncio
async def test_prepared_journal_is_discarded_when_file_was_not_written(project):
    path = "paper.md"
    settings.get_project_path(project).joinpath(path).write_text("old")
    loaded = await annotation_service.get_annotations(project, path)
    async with UnitOfWork(project) as uow:
        await uow.annotations.create_transaction(
            id=generate_id(), file_path=path,
            expected_revision=loaded["revision"], expected_file_hash=loaded["fileHash"],
            new_file_hash=file_service.compute_hash("new"), old_content="old",
            mutations=json.dumps({"upserts": [], "deleteIds": []}),
        )
    await annotation_service.recover_transactions(project)
    async with UnitOfWork(project) as uow:
        assert await uow.annotations.get_transactions() == []


@pytest.mark.asyncio
async def test_write_failure_leaves_journal_for_next_access(project, monkeypatch):
    path = "paper.md"
    settings.get_project_path(project).joinpath(path).write_text("old")
    loaded = await annotation_service.get_annotations(project, path)

    def fail_write(*args, **kwargs):
        raise OSError("injected write failure")

    original_write = file_service.write_file_content
    monkeypatch.setattr(file_service, "write_file_content", fail_write)
    with pytest.raises(OSError):
        await annotation_service.save_document(
            project, path, "new", loaded["fileHash"], loaded["revision"], [], [],
        )
    async with UnitOfWork(project) as uow:
        assert len(await uow.annotations.get_transactions()) == 1

    monkeypatch.setattr(file_service, "write_file_content", original_write)
    await annotation_service.get_annotations(project, path)
    async with UnitOfWork(project) as uow:
        assert await uow.annotations.get_transactions() == []


@pytest.mark.asyncio
async def test_db_failure_after_file_write_recovers_without_losing_file(project, monkeypatch):
    path = "paper.md"
    settings.get_project_path(project).joinpath(path).write_text("old")
    loaded = await annotation_service.get_annotations(project, path)
    original_apply = AnnotationRepository.apply_mutation_cas
    calls = 0

    async def fail_once(repo, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("injected database conflict")
        return await original_apply(repo, *args, **kwargs)

    monkeypatch.setattr("app.database.repos.annotation_repo.AnnotationRepository.apply_mutation_cas", fail_once)
    with pytest.raises(Exception):
        await annotation_service.save_document(
            project, path, "new", loaded["fileHash"], loaded["revision"], [], [],
        )
    assert settings.get_project_path(project).joinpath(path).read_text() == "new"
    refreshed = await annotation_service.get_annotations(project, path)
    assert refreshed["fileHash"] == file_service.compute_hash("new")
    async with UnitOfWork(project) as uow:
        assert await uow.annotations.get_transactions() == []


@pytest.mark.asyncio
async def test_save_conflict_restores_file_without_applying_mutation(project, monkeypatch):
    path = "paper.md"
    settings.get_project_path(project).joinpath(path).write_text("old")
    loaded = await annotation_service.get_annotations(project, path)
    original_apply = AnnotationRepository.apply_mutation_cas
    other_id = generate_id()
    requested_id = generate_id()

    injected = False

    async def apply_after_other_mutation(repository, *args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            await original_apply(
                repository, path,
                [{"id": other_id, "from": 0, "to": 3, "originalText": "old"}],
                [], loaded["revision"], loaded["fileHash"],
            )
        return await original_apply(repository, *args, **kwargs)

    monkeypatch.setattr(AnnotationRepository, "apply_mutation_cas", apply_after_other_mutation)
    with pytest.raises(AnnotationConflictError) as caught:
        await annotation_service.save_document(
            project, path, "new", loaded["fileHash"], loaded["revision"],
            [{"id": requested_id, "from": 0, "to": 3, "originalText": "new"}], [],
        )
    assert caught.value.status_code == 409
    assert settings.get_project_path(project).joinpath(path).read_text() == "old"
    refreshed = await annotation_service.get_annotations(project, path)
    assert [item["id"] for item in refreshed["annotations"]] == [other_id]


@pytest.mark.asyncio
async def test_failed_compensation_keeps_journal_until_next_access(project, monkeypatch):
    path = "paper.md"
    settings.get_project_path(project).joinpath(path).write_text("old")
    loaded = await annotation_service.get_annotations(project, path)
    transaction_id = generate_id()
    new_hash = file_service.compute_hash("new")
    async with UnitOfWork(project, immediate=True) as uow:
        await uow.annotations.create_transaction(
            id=transaction_id, file_path=path,
            expected_revision=loaded["revision"], expected_file_hash=loaded["fileHash"],
            new_file_hash=new_hash, old_content="old",
            mutations=json.dumps({"upserts": [], "deleteIds": []}),
        )
        await uow.annotations.apply_mutation_cas(
            path, [{"id": generate_id(), "from": 0, "to": 3, "originalText": "old"}],
            [], loaded["revision"], loaded["fileHash"],
        )
    settings.get_project_path(project).joinpath(path).write_text("new")
    original_write = file_service.write_file_content

    def fail_restore(*args, **kwargs):
        return {"conflict": True}

    monkeypatch.setattr(file_service, "write_file_content", fail_restore)
    await annotation_service.recover_transactions(project)
    async with UnitOfWork(project) as uow:
        assert len(await uow.annotations.get_transactions()) == 1

    monkeypatch.setattr(file_service, "write_file_content", original_write)
    await annotation_service.get_annotations(project, path)
    assert settings.get_project_path(project).joinpath(path).read_text() == "old"
    async with UnitOfWork(project) as uow:
        assert await uow.annotations.get_transactions() == []


@pytest.mark.asyncio
async def test_journal_is_cleared_after_db_commit_before_delete_window(project, monkeypatch):
    path = "paper.md"
    settings.get_project_path(project).joinpath(path).write_text("old")
    loaded = await annotation_service.get_annotations(project, path)
    original_delete = AnnotationRepository.delete_transaction
    calls = 0

    async def fail_delete(repo, transaction_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected post-commit exit")
        return await original_delete(repo, transaction_id)

    monkeypatch.setattr(AnnotationRepository, "delete_transaction", fail_delete)
    with pytest.raises(OSError):
        await annotation_service.save_document(
            project, path, "new", loaded["fileHash"], loaded["revision"], [], [],
        )
    assert settings.get_project_path(project).joinpath(path).read_text() == "new"
    await annotation_service.get_annotations(project, path)
    async with UnitOfWork(project) as uow:
        assert await uow.annotations.get_transactions() == []


@pytest.mark.asyncio
async def test_restore_race_keeps_journal_as_recovery_evidence(project, monkeypatch):
    path = "paper.md"
    full_path = settings.get_project_path(project) / path
    full_path.write_text("old")
    loaded = await annotation_service.get_annotations(project, path)
    transaction_id = generate_id()
    new_hash = file_service.compute_hash("new")
    async with UnitOfWork(project, immediate=True) as uow:
        await uow.annotations.create_transaction(
            id=transaction_id, file_path=path,
            expected_revision=loaded["revision"], expected_file_hash=loaded["fileHash"],
            new_file_hash=new_hash, old_content="old",
            mutations=json.dumps({"upserts": [], "deleteIds": []}),
        )
        await uow.annotations.apply_mutation_cas(
            path, [{"id": generate_id(), "from": 0, "to": 3, "originalText": "old"}],
            [], loaded["revision"], loaded["fileHash"],
        )
    full_path.write_text("new")
    original_write = file_service.write_file_content

    def restore_then_replace(*args, **kwargs):
        result = original_write(*args, **kwargs)
        full_path.write_text("someone else")
        return result

    monkeypatch.setattr(file_service, "write_file_content", restore_then_replace)
    await annotation_service.recover_transactions(project)

    assert full_path.read_text() == "someone else"
    async with UnitOfWork(project) as uow:
        assert await uow.annotations.get_transactions() == []


@pytest.mark.asyncio
async def test_delete_waits_for_drain_and_keeps_annotation_on_timeout(project, monkeypatch):
    path = "paper.md"
    settings.get_project_path(project).joinpath(path).write_text("hello")
    created = await annotation_service.add_annotation(project, path, 0, 5, "note")
    loaded = await annotation_service.get_annotations(project, path)
    drained = False

    async def drain(_project_id, _annotation_ids):
        nonlocal drained
        drained = True

    monkeypatch.setattr(annotation_service, "_drain_annotation_tasks", drain)
    result = await annotation_service.delete_annotation(
        project, created["id"], loaded["revision"], loaded["fileHash"],
    )
    assert result["deleted"] is True
    assert drained

    created = await annotation_service.add_annotation(project, path, 0, 5, "note")
    loaded = await annotation_service.get_annotations(project, path)

    async def timeout(_project_id, _annotation_ids):
        raise TaskActiveError(task_id="running")

    monkeypatch.setattr(annotation_service, "_drain_annotation_tasks", timeout)
    with pytest.raises(TaskActiveError):
        await annotation_service.delete_annotation(
            project, created["id"], loaded["revision"], loaded["fileHash"],
        )
    refreshed = await annotation_service.get_annotations(project, path)
    assert [item["id"] for item in refreshed["annotations"]] == [created["id"]]
