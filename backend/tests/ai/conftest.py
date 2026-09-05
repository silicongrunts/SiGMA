"""Shared fake database-boundary stand-ins for the tests/ai service units.

One parameterized fake per boundary replaces the near-identical per-file
UnitOfWork / repository stubs: ``FakeUnitOfWork`` hands out repos as class
attributes (``make_fake_uow`` builds a per-test subclass), and the small
repo fakes mirror the surface the services actually touch. Fakes with a
genuinely different contract (e.g. the chat-search repo's SQLite ASCII
case-folding pre-filter) stay in their test module with a comment saying
why. Import them as ``from tests.ai.conftest import ...``.
"""

from types import SimpleNamespace


class FakeUnitOfWork:
    """Stand-in for ``app.database.unit_of_work.UnitOfWork``.

    Repos live on the class (build a per-test subclass via
    :func:`make_fake_uow`) and are exposed under the real UnitOfWork's
    attribute names, for both ``async with UnitOfWork(pid) as uow`` and
    ``UnitOfWork.execute_atomic``. Flags:

    - ``run_atomic_operation=False``: the atomic operation must never run
      (cancelled compaction paths) — execute_atomic records and returns.
    - ``fail_atomic=True``: the operation runs, then the "commit" raises —
      staged writes happened but the transaction broke.
    - ``atomic_calls``: set to a list to record execute_atomic project ids.
    """

    sessions = None
    messages = None
    annotations = None
    task_state = None
    tasks = None
    config = None

    fail_atomic = False
    run_atomic_operation = True
    atomic_calls = None

    def __init__(self, project_id=None, **_kwargs):
        self.project_id = project_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    @classmethod
    async def execute_atomic(cls, project_id, operation, immediate=False):
        if cls.atomic_calls is not None:
            cls.atomic_calls.append(project_id)
        if not cls.run_atomic_operation:
            return None
        result = await operation(cls(project_id))
        if cls.fail_atomic:
            raise RuntimeError("commit failed")  # staged, then the commit broke
        return result


def make_fake_uow(**overrides):
    """Build a FakeUnitOfWork subclass with per-test repos/flags."""
    return type("FakeUnitOfWork", (FakeUnitOfWork,), overrides)


class FakeConfigRepo:
    """config repo stand-in: every key falls back to its default."""

    async def get(self, key, default=""):
        return default


class FakeTaskStateRepo:
    """task_state repo stand-in for the active-read and cancel surfaces.

    ``read_failures``/``cancel_failures`` raise ``read_error``/``cancel_error``
    on that many leading calls before the real behavior kicks in — the
    transient locked-DB and flaky-read scenarios.
    """

    def __init__(
        self,
        active=None,
        cancel_status=None,
        row=None,
        read_failures=0,
        cancel_failures=0,
        read_error=None,
        cancel_error=None,
    ):
        self._active = active
        self._cancel_status = cancel_status
        self._row = row
        self._read_failures = read_failures
        self._cancel_failures = cancel_failures
        self._read_error = read_error
        self._cancel_error = cancel_error
        self.get_active_calls = 0
        self.request_cancel_calls = 0
        self.requested = []
        self.mark_cancelled_calls = []
        self.get_by_id_calls = []

    async def get_active_by_session(self, session_id):
        self.get_active_calls += 1
        if self.get_active_calls <= self._read_failures:
            raise self._read_error
        return self._active

    async def request_cancel(self, task_id):
        self.request_cancel_calls += 1
        self.requested.append(task_id)
        if self.request_cancel_calls <= self._cancel_failures:
            raise self._cancel_error
        return self._cancel_status

    async def mark_cancelled(self, task_id):
        self.mark_cancelled_calls.append(task_id)

    async def get_by_id(self, task_id):
        self.get_by_id_calls.append(task_id)
        return self._row

    async def cancel_for_sessions(self, session_ids, *, commit=True):
        return []

class _StagedSessionRow(SimpleNamespace):
    """Staged session row carrying a ``to_dict()`` like real repo rows."""

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if k != "to_dict"}


class FakeSessionRepo:
    """sessions repo stand-in covering lookup, descendant collection,
    staged creates, and deletes."""

    def __init__(self, sessions=None, descendants=None):
        self.sessions = sessions if sessions is not None else {}
        self.descendants = descendants if descendants is not None else {}
        self.staged = []
        self.deleted = []

    async def get_by_id(self, session_id):
        return self.sessions.get(session_id)

    async def assert_writable(self, session_id):
        row = self.sessions.get(session_id)
        return row

    async def begin_delete(self, session_id, *, commit=True):
        if session_id not in self.descendants:
            return [], []
        task_ids = []
        return self.descendants[session_id], task_ids

    async def delete_marked(self, session_id):
        return await self.delete(session_id)

    async def collect_descendant_session_ids(self, session_id):
        return self.descendants[session_id]

    async def stage_create(self, **fields):
        self.staged.append(fields)
        row = _StagedSessionRow(
            id=fields.get("session_id"),
            **{k: v for k, v in fields.items() if k != "session_id"},
        )
        self.sessions[row.id] = row
        return row

    async def delete(self, session_id):
        self.deleted.append(session_id)
        return True


class FakeSessionTempService:
    """session_temp_service stand-in recording dir copies/deletes, with
    failure injection for the cleanup-path tests."""

    def __init__(self, fail_delete_on=None, fail_copy_after=None):
        self.copied = []
        self.deleted = []
        self.fail_delete_on = fail_delete_on
        self.fail_copy_after = fail_copy_after

    def copy_session_dir(self, project_id, src, dst):
        if (
            self.fail_copy_after is not None
            and len(self.copied) >= self.fail_copy_after
        ):
            raise RuntimeError("disk full")
        self.copied.append((src, dst))

    def delete_session_dir(self, project_id, session_id):
        if session_id == self.fail_delete_on:
            raise OSError("rmtree failed")
        self.deleted.append(session_id)


class RecordingMessagesRepo:
    """messages repo stand-in recording staged writes instead of touching a
    database; ``history`` is what ``get_messages_for_llm`` hands back."""

    def __init__(self, history=None):
        self._history = list(history or [])
        self.created = []
        self.updated = []

    async def get_messages_for_llm(self, session_id):
        return list(self._history)

    async def get_messages(self, session_id):
        return list(self._history)

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id="new-message")

    async def stage_create(self, **kwargs):
        return await self.create(**kwargs)

    async def stage_update_content(self, message_id, **kwargs):
        self.updated.append((message_id, kwargs))
        return True


class RecordingSessionsRepo:
    """sessions repo stand-in recording touch/stage_touch calls."""

    def __init__(self):
        self.touched = []

    async def touch(self, session_id):
        pass

    async def stage_touch(self, session_id):
        self.touched.append(session_id)
