"""Chat search: visibility filtering, grouping, title matches, and caps."""

from types import SimpleNamespace

import pytest

from app.core.exceptions import ValidationError
import app.services.ai_service as ai_service_module


def _msg(mid, seq, role, content="", tool_calls=None):
    return SimpleNamespace(
        id=mid, seq=seq, role=role, content=content,
        tool_calls=tool_calls, tool_call_id=None, reasoning_content=None,
        token_count=0, cached_tokens=0, input_tokens=0,
        is_boundary=False, created_at=None,
    )


class _FakeSessionRow(SimpleNamespace):
    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "is_archived": getattr(self, "is_archived", False),
        }


class _FakeSessionRepo:
    def __init__(self, sessions):
        self.sessions = sessions

    async def list_all(self, include_archived=False):
        if include_archived:
            return list(self.sessions)
        return [s for s in self.sessions if not s.is_archived]


def _sqlite_lower(text):
    """ASCII-only lowering, mirroring the SQLite ``lower`` used by the real repo."""
    return "".join(
        chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in text
    )


class _FakeMessageRepo:
    def __init__(self, messages_by_session):
        self.messages_by_session = messages_by_session

    async def search_session_ids_containing(self, session_ids, needle):
        # Mirrors the real repo contract: coarse raw-row substring pre-filter
        # over user/assistant rows with SQLite's ASCII-only case folding.
        # For needles with non-ASCII cased letters this can miss visible
        # hits — which is exactly why ai_service must skip it for them.
        hits = []
        sql_needle = needle.lower()
        for sid in session_ids:
            for m in self.messages_by_session.get(sid, []):
                if m.role in ("user", "assistant") and sql_needle in _sqlite_lower(m.content or ""):
                    hits.append(sid)
                    break
        return hits

    async def get_messages_with_boundary(self, session_id):
        return list(self.messages_by_session.get(session_id, [])), None


class _FakeUnitOfWork:
    """Stand-in for UnitOfWork: hands out one fake UoW per context."""

    session_repo = None
    message_repo = None

    def __init__(self, project_id):
        self.uow = SimpleNamespace(
            sessions=self.session_repo, messages=self.message_repo,
        )

    async def __aenter__(self):
        return self.uow

    async def __aexit__(self, *args):
        return False


def _install(monkeypatch, sessions, messages_by_session):
    _FakeUnitOfWork.session_repo = _FakeSessionRepo(sessions)
    _FakeUnitOfWork.message_repo = _FakeMessageRepo(messages_by_session)
    monkeypatch.setattr(ai_service_module, "UnitOfWork", _FakeUnitOfWork)


def _session(sid, title, is_archived=False):
    return _FakeSessionRow(id=sid, title=title, is_archived=is_archived)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_matches_visible_bubbles_across_active_and_archived(monkeypatch):
    active = _session("s1", "Research chat")
    archived = _session("s2", "Old chat", is_archived=True)
    active_messages = [
        _msg("m0", 0, "user", "tell me about quantum dots"),
        _msg("m1", 1, "assistant", "", tool_calls='[{"id": "c1", "function": {"name": "search", "arguments": "{}"}}]'),
        _msg("m2", 2, "tool", "tool result"),
        _msg("m3", 3, "assistant", "Quantum dots are tiny semiconductors."),
    ]
    archived_messages = [_msg("m4", 0, "user", "quantum entanglement question")]
    _install(monkeypatch, [active, archived], {
        "s1": active_messages, "s2": archived_messages,
    })

    result = await ai_service_module.ai_service.search_chat("p1", "quantum")

    assert result["total_sessions"] == 2
    assert result["total_matches"] == 3
    assert [g["session"]["id"] for g in result["groups"]] == ["s1", "s2"]
    active_group = result["groups"][0]
    # The assistant turn collapses to one entry anchored at its first row.
    assert [(m["id"], m["role"]) for m in active_group["matches"]] == [
        ("m0", "user"), ("m1", "SiGMA"),
    ]
    assert result["groups"][1]["session"]["is_archived"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_drops_internal_tag_and_process_only_hits(monkeypatch):
    session = _session("s1", "Unrelated title")
    messages = [
        # Needle only inside the internal status tag, stripped before UI.
        _msg("m0", 0, "user", "<status>quantum search running</status>real question"),
        # Needle only in intermediate assistant text preceding tool calls —
        # process content, not the final bubble.
        _msg("m1", 1, "assistant", "checking quantum sources", tool_calls='[{"id": "c1", "function": {"name": "search", "arguments": "{}"}}]'),
        _msg("m2", 2, "tool", "tool result"),
        _msg("m3", 3, "assistant", "final answer without the needle"),
    ]
    _install(monkeypatch, [session], {"s1": messages})

    result = await ai_service_module.ai_service.search_chat("p1", "quantum")

    assert result["groups"] == []
    assert result["total_matches"] == 0
    assert result["total_sessions"] == 1  # pre-filter candidate, then dropped


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_non_ascii_query_scans_past_ascii_only_prefilter(monkeypatch):
    # "École" uppercases a non-ASCII letter; SQLite's lower() keeps it, so the
    # pre-filter misses. The service must fall back to scanning every session
    # so message matching stays as case-insensitive as the title match.
    session = _session("s1", "School notes")
    _install(monkeypatch, [session], {"s1": [_msg("m0", 0, "user", "About École")]})

    result = await ai_service_module.ai_service.search_chat("p1", "école")

    assert result["total_sessions"] == 1
    assert [m["id"] for m in result["groups"][0]["matches"]] == ["m0"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_title_only_match_returns_group_without_matches(monkeypatch):
    session = _session("s1", "Quantum notes")
    _install(monkeypatch, [session], {"s1": [_msg("m0", 0, "user", "hello")]})

    result = await ai_service_module.ai_service.search_chat("p1", "notes")

    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert group["title_match"] is True
    assert group["matches"] == []
    assert group["match_count"] == 0
    assert result["total_matches"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_rejects_blank_query(monkeypatch):
    _install(monkeypatch, [], {})
    with pytest.raises(ValidationError):
        await ai_service_module.ai_service.search_chat("p1", "   ")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_caps_matches_per_session_and_sessions_total(monkeypatch):
    # 7 visible matches in one session → 5 shown, match_count keeps 7.
    busy = _session("s-busy", "Busy")
    busy_messages = [
        _msg(f"u{i}", i * 2, "user", f"question {i} about needle")
        for i in range(7)
    ]
    # 25 sessions with one hit each → only the first 20 become groups.
    many = [_session(f"s{i}", f"Session {i}") for i in range(25)]
    messages = {"s-busy": busy_messages}
    for s in many:
        messages[s.id] = [_msg(f"{s.id}-m0", 0, "user", "needle here")]
    _install(monkeypatch, [busy] + many, messages)

    result = await ai_service_module.ai_service.search_chat("p1", "needle")

    assert result["total_sessions"] == 26
    # Totals describe the shaped window: the busy session's 7 plus the 19
    # single-hit sessions that fit inside the 20-group cap. Candidates
    # beyond the cap are never shaped, so their hits stay uncounted.
    assert result["total_matches"] == 7 + 19
    assert len(result["groups"]) == ai_service_module.ai_service.SEARCH_MAX_SESSIONS
    busy_group = next(g for g in result["groups"] if g["session"]["id"] == "s-busy")
    assert len(busy_group["matches"]) == ai_service_module.ai_service.SEARCH_MAX_MATCHES_PER_SESSION
    assert busy_group["match_count"] == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_empty_project_returns_empty_shape(monkeypatch):
    _install(monkeypatch, [], {})

    result = await ai_service_module.ai_service.search_chat("p1", "anything")

    assert result == {
        "query": "anything", "groups": [],
        "total_matches": 0, "total_sessions": 0,
    }
