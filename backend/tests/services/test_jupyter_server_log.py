"""Jupyter server logs land in a file, not in an undrained pipe.

The server's stdout/stderr were captured with subprocess.PIPE but never read,
which hid tracebacks from operators and would eventually block the server once
the pipe buffer filled.
"""

from pathlib import Path

import pytest

from app.services.jupyter_service import JupyterService


class _FakeProcess:
    def __init__(self, exit_code=None):
        self._exit_code = exit_code

    def poll(self):
        return self._exit_code


async def _true():
    return True


async def _false():
    return False


def _make_service(tmp_path: Path) -> JupyterService:
    """A service whose runtime dir (config + log) lives in *tmp_path*."""
    service = JupyterService(str(tmp_path))
    service.runtime_dir = tmp_path / "jupyter"
    service.log_path = service.runtime_dir / "jupyter_server.log"
    return service


@pytest.mark.asyncio
@pytest.mark.regression
async def test_start_redirects_server_output_to_log_file(monkeypatch, tmp_path):
    """The server's stdout/stderr are appended to a log file and the parent
    closes its handle after spawning."""
    service = _make_service(tmp_path)
    monkeypatch.setattr(service, "_port_open", _false)
    monkeypatch.setattr(service, "_check_our_token", _true)

    captured = {}

    def _fake_popen(_cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProcess(exit_code=None)

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    await service.start()

    assert "stdout" in captured and "stderr" in captured
    for stream in (captured["stdout"], captured["stderr"]):
        assert stream.name.endswith("jupyter_server.log")
        assert stream.closed
    assert service.log_path.exists()


@pytest.mark.asyncio
@pytest.mark.regression
async def test_server_log_rotated_when_oversized(monkeypatch, tmp_path):
    """A log grown past the size cap is rotated aside on the next start."""
    service = _make_service(tmp_path)
    service.runtime_dir.mkdir(parents=True)
    service.log_path.write_bytes(b"0123456789")
    monkeypatch.setattr("app.services.jupyter_service._SERVER_LOG_MAX_BYTES", 8)

    service._open_server_log().close()

    rotated = service.log_path.with_name(service.log_path.name + ".1")
    assert rotated.read_bytes() == b"0123456789"
    assert service.log_path.read_bytes() == b""


@pytest.mark.asyncio
@pytest.mark.regression
async def test_server_log_kept_when_small(tmp_path):
    """A log under the size cap is appended to, not rotated."""
    service = _make_service(tmp_path)
    service.runtime_dir.mkdir(parents=True)
    service.log_path.write_bytes(b"tiny")

    service._open_server_log().close()

    assert not service.log_path.with_name(service.log_path.name + ".1").exists()
    assert service.log_path.read_bytes() == b"tiny"


@pytest.mark.regression
def test_startup_failure_reports_server_log_tail(tmp_path, caplog):
    """When the server fails to start, the reported error includes the log."""
    service = _make_service(tmp_path)
    service.runtime_dir.mkdir(parents=True)
    service.log_path.write_bytes(b"AttributeError: boom")

    with pytest.raises(RuntimeError):
        service._raise_startup_failure()

    assert "AttributeError: boom" in caplog.text


@pytest.mark.regression
def test_missing_server_log_yields_empty_tail(tmp_path):
    """A missing log file is tolerated (best-effort reporting)."""
    service = _make_service(tmp_path)
    assert service._read_server_log_tail() == ""
