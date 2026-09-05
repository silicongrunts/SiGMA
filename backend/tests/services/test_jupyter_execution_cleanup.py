import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import app.services.jupyter_service as jupyter_module
from app.services.jupyter_service import JupyterService


class ExecutionSocket:
    close_code = None

    def __init__(self, complete=False):
        self.started = asyncio.Event()
        self.request = None
        self.complete = complete

    async def send(self, message):
        self.request = json.loads(message)
        self.started.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.complete:
            await asyncio.Event().wait()
        return json.dumps({
            "parent_header": {"msg_id": self.request["header"]["msg_id"]},
            "msg_type": "execute_reply", "channel": "shell",
            "content": {"status": "ok", "execution_count": 1},
        })

    async def close(self):
        self.close_code = 1000


@pytest.fixture
def execution(tmp_path, monkeypatch):
    service = JupyterService(str(tmp_path))
    socket = ExecutionSocket()
    monkeypatch.setattr(jupyter_module.websockets, "connect", AsyncMock(return_value=socket))
    monkeypatch.setattr(service, "interrupt_kernel", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "get_kernel_status", AsyncMock(return_value={"execution_state": "idle"}))
    monkeypatch.setattr(service, "kill_kernel", AsyncMock())
    monkeypatch.setattr(jupyter_module, "_EXECUTION_INTERRUPT_GRACE", 0.05)
    monkeypatch.setattr(jupyter_module, "_EXECUTION_KILL_TIMEOUT", 0.05)
    return service, socket


@pytest.mark.asyncio
async def test_cancel_interrupts_execution_without_killing_idle_shared_kernel(execution):
    service, socket = execution
    task = asyncio.create_task(service.execute_code("kernel", "work()", project_id="project", session_id="session"))
    await socket.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    service.interrupt_kernel.assert_awaited_once_with("kernel")
    service.kill_kernel.assert_not_awaited()
    assert service._executions == {}
    assert socket.close_code == 1000


@pytest.mark.asyncio
async def test_timeout_kills_only_the_nonresponsive_kernel(execution):
    service, socket = execution
    service.get_kernel_status.return_value = {"execution_state": "busy"}
    result = await service.execute_code("kernel", "work()", timeout=0.01)
    assert result["status"] == "timeout"
    service.kill_kernel.assert_awaited_once_with("kernel")
    assert service._executions == {}
    assert socket.close_code == 1000


@pytest.mark.asyncio
async def test_repeated_cancel_does_not_orphan_kernel_cleanup(execution):
    service, socket = execution
    interrupt_started = asyncio.Event()
    release = asyncio.Event()

    async def interrupt(_kernel):
        interrupt_started.set()
        await release.wait()
        return True

    service.interrupt_kernel.side_effect = interrupt
    task = asyncio.create_task(service.execute_code("kernel", "work()"))
    await socket.started.wait()
    task.cancel()
    await interrupt_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service._executions == {}
    assert socket.close_code == 1000


@pytest.mark.asyncio
async def test_failed_cleanup_is_retried_by_session_deletion(execution):
    service, socket = execution
    service.interrupt_kernel.return_value = False
    service.kill_kernel.side_effect = RuntimeError("API unavailable")
    with pytest.raises(RuntimeError, match="API unavailable"):
        await service.execute_code("kernel", "work()", timeout=0.01, project_id="project", session_id="session")
    execution_owner = service._executions["kernel"]
    assert (execution_owner.project_id, execution_owner.session_id) == ("project", "session")
    assert not execution_owner.running
    assert socket.close_code == 1000
    service.kill_kernel.reset_mock(side_effect=True)
    await service.stop_session_executions("other-project", ["session"])
    service.kill_kernel.assert_not_awaited()
    await service.stop_session_executions("project", ["session"])
    service.kill_kernel.assert_awaited_once_with("kernel")
    assert service._executions == {}


@pytest.mark.asyncio
async def test_completed_execution_keeps_shared_kernel_alive(execution):
    service, socket = execution
    socket.complete = True
    assert (await service.execute_code("kernel", "work()"))["status"] == "ok"
    service.interrupt_kernel.assert_not_awaited()
    service.kill_kernel.assert_not_awaited()
    assert service._executions == {}


@pytest.mark.asyncio
async def test_connection_failure_does_not_interrupt_unowned_kernel(execution, monkeypatch):
    service, _ = execution
    monkeypatch.setattr(jupyter_module.websockets, "connect", AsyncMock(side_effect=ConnectionRefusedError))
    result = await service.execute_code("kernel", "work()")
    assert result["status"] == "error"
    service.interrupt_kernel.assert_not_awaited()
    assert service._executions == {}


@pytest.mark.asyncio
async def test_stop_does_not_release_live_execution_before_its_runner_exits(execution):
    service, socket = execution
    task = asyncio.create_task(service.execute_code("kernel", "work()"))
    await socket.started.wait()
    try:
        await service.stop_execution("kernel")
        with pytest.raises(RuntimeError, match="still active"):
            await service.execute_code("kernel", "next_work()")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert service._executions == {}


@pytest.mark.asyncio
async def test_unresponsive_kill_is_bounded_and_remains_recoverable(execution):
    from app.core.exceptions import JupyterKernelError

    service, _ = execution
    service.interrupt_kernel.return_value = False

    async def unresponsive_kill(_kernel):
        await asyncio.Event().wait()

    service.kill_kernel.side_effect = unresponsive_kill
    with pytest.raises(JupyterKernelError, match="termination could not be confirmed"):
        await asyncio.wait_for(
            service.execute_code("kernel", "work()", timeout=0.01), timeout=1,
        )
    assert not service._executions["kernel"].running


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
async def test_execution_rejects_unbounded_or_invalid_timeout(execution, timeout):
    service, _ = execution
    with pytest.raises(ValueError, match="positive finite"):
        await service.execute_code("kernel", "work()", timeout=timeout)
    assert service._executions == {}


@pytest.mark.asyncio
async def test_long_execution_timeout_remains_supported(execution):
    service, socket = execution
    socket.complete = True
    assert (await service.execute_code("kernel", "work()", timeout=3600))["status"] == "ok"
