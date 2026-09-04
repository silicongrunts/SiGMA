"""Docling conversion channel behavior.

Pins the pipe handoff between the web process and the conversion worker:
large results are transferred completely off the event loop, a worker that
dies without sending a result surfaces as a conversion failure, and a
worker that never starts leaves both pipe ends closed.
"""

import asyncio
import multiprocessing
import multiprocessing.connection
import os
import threading
import time
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.exceptions import DocumentConversionError
from app.services.document_processing_service import DocumentProcessingService


class ThreadProcess:
    """Runs the worker target in a thread so tests avoid spawning interpreters.

    The "child" gets its own duplicated file descriptor for every Connection
    argument, matching how a real spawned process receives the pipe.
    """

    def __init__(self, target, args):
        self._target = target
        self._args = args
        self._thread = None

    def start(self):
        child_args = tuple(
            multiprocessing.connection.Connection(
                os.dup(arg.fileno()),
                readable=arg.readable,
                writable=arg.writable,
            ) if isinstance(arg, multiprocessing.connection.Connection) else arg
            for arg in self._args
        )
        self._thread = threading.Thread(
            target=self._target, args=child_args, daemon=True,
        )
        self._thread.start()

    def is_alive(self):
        return self._thread.is_alive()

    def terminate(self):
        # A worker that already sent its result may still be winding down
        # when the caller terminates it; a real process just exits on the
        # signal, so the fake lets the thread finish.
        pass

    def kill(self):
        pass

    def join(self, timeout=None):
        self._thread.join(timeout=timeout)


class SpawnContext:
    """Mimics a spawn context but runs ``worker`` instead of the real one."""

    def __init__(self, worker):
        self._worker = worker

    def Pipe(self, duplex=False):
        assert duplex is False
        return multiprocessing.Pipe(duplex=False)

    def Process(self, target, args, daemon=None):
        return ThreadProcess(self._worker, args)


def _service(monkeypatch, ctx):
    svc = DocumentProcessingService()
    monkeypatch.setattr(
        "app.services.document_processing_service.multiprocessing",
        SimpleNamespace(get_context=lambda name: ctx),
    )

    async def no_stop(*args, **kwargs):
        return False

    monkeypatch.setattr(svc, "_should_stop", no_stop)
    return svc


@pytest.mark.asyncio
async def test_convert_returns_large_worker_payload(monkeypatch):
    """A payload larger than the pipe buffer is transferred completely."""
    payload = "x" * (1024 * 1024)

    def worker(file_path, conn):
        conn.send((True, payload))
        conn.close()

    svc = _service(monkeypatch, SpawnContext(worker))

    result = await svc._convert_with_docling("paper.pdf", "proj", "doc1")

    assert result == payload


@pytest.mark.asyncio
async def test_worker_death_without_result_is_conversion_error(monkeypatch):
    def worker(file_path, conn):
        conn.close()

    svc = _service(monkeypatch, SpawnContext(worker))

    with pytest.raises(DocumentConversionError):
        await svc._convert_with_docling("paper.pdf", "proj", "doc1")


@pytest.mark.asyncio
async def test_process_start_failure_closes_both_pipe_ends(monkeypatch):
    closed = []

    class FakeConn:
        def close(self):
            closed.append(self)

    class FailingProcess:
        def start(self):
            raise OSError("resource exhausted")

    class FailingContext:
        def Pipe(self, duplex=False):
            return FakeConn(), FakeConn()

        def Process(self, target, args, daemon=None):
            return FailingProcess()

    svc = _service(monkeypatch, FailingContext())

    with pytest.raises(OSError):
        await svc._convert_with_docling("paper.pdf", "proj", "doc1")

    assert len(closed) == 2


# ---------------------------------------------------------------------------
# Per-conversion timeout: a child stuck in native code must not starve the
# library queue; it is terminated and surfaces as a conversion failure.
# ---------------------------------------------------------------------------


class HangingContext:
    """Spawn-like context whose worker never produces a result.

    The worker only exits once the caller terminates (or kills) it, mimicking
    native code stuck past any poll deadline.
    """

    def __init__(self):
        self.terminated = threading.Event()

    def Pipe(self, duplex=False):
        assert duplex is False
        return multiprocessing.Pipe(duplex=False)

    def Process(self, target, args, daemon=None):
        outer = self

        class _Process:
            def start(self):
                self._thread = threading.Thread(
                    target=lambda: outer.terminated.wait(10), daemon=True,
                )
                self._thread.start()

            def is_alive(self):
                return self._thread.is_alive()

            def terminate(self):
                outer.terminated.set()

            def kill(self):
                outer.terminated.set()

            def join(self, timeout=None):
                self._thread.join(timeout=timeout)

        return _Process()


@pytest.mark.asyncio
async def test_conversion_hanging_past_timeout_is_killed_and_fails(monkeypatch):
    ctx = HangingContext()
    svc = _service(monkeypatch, ctx)
    monkeypatch.setattr(settings.library, "conversion_timeout_seconds", 0.05)

    start = time.monotonic()
    with pytest.raises(DocumentConversionError):
        await svc._convert_with_docling("paper.pdf", "proj", "doc1")
    elapsed = time.monotonic() - start

    assert elapsed < 5
    assert ctx.terminated.is_set()  # the hung worker was killed, not leaked


# ---------------------------------------------------------------------------
# Timeout cleanup: a child that ignores terminate is killed, and the recv
# thread abandoned by wait_for is drained before the pipe is closed.
# ---------------------------------------------------------------------------


class StubbornChildContext:
    """Context whose child ignores terminate and whose pipe has a recv stuck
    mid-transfer — the exact state a timed-out conversion leaves behind.

    ``poll`` reports readable bytes immediately, but ``recv`` blocks like a
    large unpickle and only returns once the child dies (a dead child closes
    its pipe end, surfacing as EOF), mirroring a real pipe between threads.
    """

    def __init__(self):
        self.child_dead = threading.Event()
        self.kill_called = threading.Event()
        # Ordered proof that recv was drained before recv_conn.close().
        self.events = []

    def Pipe(self, duplex=False):
        outer = self
        assert duplex is False

        class _RecvConn:
            def poll(self, timeout):
                return True  # bytes are readable mid-transfer

            def recv(self):
                outer.child_dead.wait(10)
                outer.events.append("recv_returned")
                raise EOFError("child died mid-transfer")

            def close(self):
                outer.events.append("closed")

        class _SendConn:
            def close(self):
                pass

        return _RecvConn(), _SendConn()

    def Process(self, target, args, daemon=None):
        outer = self

        class _Process:
            def start(self):
                self._thread = threading.Thread(
                    target=outer.child_dead.wait, args=(10,), daemon=True,
                )
                self._thread.start()

            def is_alive(self):
                return self._thread.is_alive()

            def terminate(self):
                pass  # stuck in native code — terminate must not suffice

            def kill(self):
                outer.kill_called.set()
                outer.child_dead.set()

            def join(self, timeout=None):
                self._thread.join(timeout=timeout)

        return _Process()


@pytest.mark.asyncio
async def test_timeout_kills_stubborn_child_and_drains_recv_before_close(monkeypatch):
    ctx = StubbornChildContext()
    svc = _service(monkeypatch, ctx)
    monkeypatch.setattr(settings.library, "conversion_timeout_seconds", 0.05)
    monkeypatch.setattr(
        "app.services.document_processing_service._REAP_JOIN_SECONDS", 0.05,
    )

    loop = asyncio.get_running_loop()
    handler_surprises = []
    previous_handler = loop.get_exception_handler()

    def record_surprises(_loop, context):
        handler_surprises.append(context.get("message"))

    loop.set_exception_handler(record_surprises)
    try:
        with pytest.raises(DocumentConversionError):
            await svc._convert_with_docling("paper.pdf", "proj", "doc1")
        await asyncio.sleep(0.05)  # let any unretrieved-exception report fire
    finally:
        loop.set_exception_handler(previous_handler)

    assert ctx.kill_called.is_set()  # terminate didn't stop the child; kill did
    assert ctx.events == ["recv_returned", "closed"]  # recv drained, then closed
    assert handler_surprises == []  # no "Future exception was never retrieved"


# ---------------------------------------------------------------------------
# Lease loss: a task whose ownership was reclaimed stops its conversion.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversion_stops_when_task_loses_lease(monkeypatch):
    def worker(file_path, conn):
        time.sleep(0.3)
        conn.send((True, "late"))
        conn.close()

    svc = DocumentProcessingService()
    monkeypatch.setattr(
        "app.services.document_processing_service.multiprocessing",
        SimpleNamespace(get_context=lambda name: SpawnContext(worker)),
    )

    class LostLeaseContext:
        async def is_cancelling(self):
            return False

        async def heartbeat(self):
            return False

    result = await svc._convert_with_docling(
        "paper.pdf", "proj", "doc1", task_context=LostLeaseContext(),
    )

    assert result is None
