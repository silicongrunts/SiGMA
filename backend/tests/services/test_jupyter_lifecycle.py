"""Jupyter project cleanup reports incomplete kernel shutdown."""

import pytest

from app.core.exceptions import JupyterKernelError
from app.services.jupyter_service import JupyterService


class _Response:
    def __init__(self, status_code, payload=None, text="error"):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _Client:
    """httpx.AsyncClient stand-in.

    All state is per-instance so tests cannot leak responses or recorded
    deletes into each other through shared class attributes.
    """

    def __init__(self, get_response=None, delete_response=None):
        self.get_response = get_response
        self.delete_response = delete_response
        self.deletes: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, _url):
        return self.get_response

    async def delete(self, url):
        self.deletes.append(url)
        return self.delete_response


class _RunningProcess:
    def poll(self):
        return None


@pytest.fixture
def install_client(monkeypatch):
    """Install a fresh ``_Client`` as the service's AsyncClient and return it."""
    def _install(get_response=None, delete_response=None):
        client = _Client(get_response, delete_response)
        monkeypatch.setattr(
            "app.services.jupyter_service.httpx.AsyncClient",
            lambda **_kwargs: client,
        )
        return client
    return _install


async def _true():
    return True


async def _false():
    return False


@pytest.mark.asyncio
async def test_kernel_list_failure_is_reported(monkeypatch, install_client):
    service = JupyterService("/tmp")
    service.process = _RunningProcess()
    monkeypatch.setattr(service, "is_running", _true)
    install_client(get_response=_Response(503))

    with pytest.raises(JupyterKernelError):
        await service.kill_project_kernels("project")


@pytest.mark.asyncio
async def test_kernel_delete_failure_is_reported(monkeypatch, install_client):
    service = JupyterService("/tmp")
    service.process = _RunningProcess()
    monkeypatch.setattr(service, "is_running", _true)
    client = install_client(
        get_response=_Response(
            200,
            payload=[{"notebook": {"path": "project/notebook.ipynb"}, "kernel": {"id": "kernel-1"}}],
        ),
        delete_response=_Response(500),
    )

    with pytest.raises(JupyterKernelError) as exc_info:
        await service.kill_project_kernels("project")

    assert exc_info.value.details["kernel_id"] == "kernel-1"
    assert len(client.deletes) == 1


@pytest.mark.asyncio
async def test_no_running_jupyter_is_a_noop(monkeypatch):
    """Without a managed process the cleanup returns early: no kernel error
    is raised and no Jupyter API client is ever opened."""
    service = JupyterService("/tmp")
    opened = []

    def _record_open(**_kwargs):
        opened.append("open")
        return _Client()

    monkeypatch.setattr(
        "app.services.jupyter_service.httpx.AsyncClient", _record_open,
    )

    assert await service.kill_project_kernels("project") is None
    assert opened == []


@pytest.mark.asyncio
async def test_live_managed_jupyter_api_failure_is_reported(monkeypatch):
    service = JupyterService("/tmp")
    service.process = _RunningProcess()
    monkeypatch.setattr(service, "is_running", _false)

    with pytest.raises(JupyterKernelError) as exc_info:
        await service.kill_project_kernels("project")

    assert exc_info.value.code == "JUPYTER_KERNEL_ERROR"
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_live_managed_jupyter_check_exception_is_reported(monkeypatch):
    service = JupyterService("/tmp")
    service.process = _RunningProcess()

    async def fail_check():
        raise TimeoutError("temporary outage")

    monkeypatch.setattr(service, "is_running", fail_check)

    with pytest.raises(JupyterKernelError) as exc_info:
        await service.kill_project_kernels("project")

    assert exc_info.value.code == "JUPYTER_KERNEL_ERROR"
    assert exc_info.value.status_code == 503
