import os
import re
import subprocess
import secrets
import asyncio
import uuid
import json as _json
from pathlib import Path
from typing import Optional

import httpx
import websockets

from app.core.config import settings
from app.core.logging import get_logger
from app.core.utils import to_iso, utcnow

logger = get_logger(__name__)

# Maximum characters for collected execution output before truncation
_MAX_EXECUTION_OUTPUT = 100_000


class JupyterService:
    """Manages a Jupyter Notebook server subprocess for embedding via iframe proxy.

    Provides high-level methods for notebook content I/O (via Contents API),
    kernel lifecycle (sessions, status), and code execution (via WebSocket).
    """

    def __init__(self, base_dir: str, port: int = 8890):
        self.base_dir = Path(base_dir).resolve()
        self.runtime_dir = settings.SIGMA_DIR / "jupyter"
        self.executable = settings.JUPYTER_BIN
        self.port = port
        self.token = secrets.token_hex(16)
        self.process: Optional[subprocess.Popen] = None
        self._config_path: Optional[Path] = None

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _api_url(self, path: str) -> str:
        """Construct a Jupyter REST API URL (with base_url prefix)."""
        return f"http://localhost:{self.port}/api/v1/jupyter/api/{path.lstrip('/')}"

    def _ws_url(self, path: str) -> str:
        """Construct a Jupyter WebSocket URL."""
        return f"ws://localhost:{self.port}/api/v1/jupyter/api/{path.lstrip('/')}"

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Ensure a Jupyter server is running and responsive with *our* token."""
        if self.process and self.process.poll() is None:
            if await self._check_our_token():
                return
            logger.warning("Jupyter on port %d has wrong token – killing", self.port)
            self._kill_process()

        if await self._port_open():
            if not await self._check_our_token():
                logger.warning("Unknown process on port %d – killing it", self.port)
                self._force_kill_port()
                await asyncio.sleep(1)
            else:
                return

        self._write_config()

        cmd = self._start_command()
        logger.info("Starting Jupyter nbclassic: %s", " ".join(cmd))

        env = os.environ.copy()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        env['JUPYTER_CONFIG_DIR'] = str(self.runtime_dir)
        env['JUPYTER_DATA_DIR'] = str(self.runtime_dir)

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(self.base_dir),
            env=env,
        )

        for _ in range(30):
            if self.process.poll() is not None:
                self._raise_startup_failure()
            if await self._check_our_token():
                logger.info("Jupyter server started on port %d", self.port)
                return
            await asyncio.sleep(1)

        self._raise_startup_failure()

    def _raise_startup_failure(self):
        stdout, stderr = "", ""
        try:
            stdout, stderr = self.process.communicate(timeout=1)
        except Exception:
            logger.debug("Failed to collect Jupyter startup output", exc_info=True)
        logger.error("Jupyter failed to start. stdout=%s stderr=%s", stdout, stderr)
        raise RuntimeError("Failed to start Jupyter server")

    async def is_running(self) -> bool:
        return await self._check_our_token()

    def get_url(self, path: str = "") -> str:
        """Return the proxy-relative URL for the embedded Jupyter iframe."""
        return f"/api/v1/jupyter/notebooks/{path}?token={self.token}"

    def stop(self):
        self._kill_process()

    def _start_command(self) -> list[str]:
        """Return the nbclassic command used for the embedded notebook UI."""
        return [
            self.executable, "nbclassic",
            f"--config={self._config_path}",
            "--no-browser",
            f"--port={self.port}",
            "--allow-root",
        ]

    # ------------------------------------------------------------------
    # Contents API — notebook read / write
    # ------------------------------------------------------------------

    async def get_notebook(self, notebook_path: str) -> Optional[dict]:
        """Read a notebook via the Jupyter Contents API.

        Args:
            notebook_path: Path relative to Jupyter root_dir
                           (e.g. "project_id/analysis.ipynb").

        Returns:
            The notebook dict (with cells, metadata, etc.) or None if
            Jupyter is not running or the file does not exist.
        """
        if not await self.is_running():
            return None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    self._api_url(f"contents/{notebook_path}?token={self.token}&content=1")
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
                if data.get("type") == "notebook":
                    return data.get("content")
                return None
        except Exception:
            logger.debug("get_notebook failed for %s", notebook_path, exc_info=True)
            return None

    async def save_notebook(self, notebook_path: str, notebook: dict) -> bool:
        """Save a notebook via the Jupyter Contents API (PUT).

        Jupyter persists to disk. The frontend reloads the embedded nbclassic
        frame after agent-driven notebook changes so the visible document is
        read back from disk.

        Returns True on success, False if Jupyter is unavailable.
        """
        if not await self.is_running():
            return False
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.put(
                    self._api_url(f"contents/{notebook_path}?token={self.token}"),
                    json={"type": "notebook", "content": notebook},
                )
                return resp.status_code == 200
        except Exception:
            logger.debug("save_notebook failed for %s", notebook_path, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Sessions & kernels
    # ------------------------------------------------------------------

    async def get_session_for_notebook(self, notebook_path: str, create: bool = True) -> Optional[dict]:
        """Find or create a Jupyter session for the given notebook.

        A session binds a kernel to a notebook path. If ``create`` is true and
        no session exists, a new one is created.

        Returns the session dict (includes ``kernel.id``) or None.
        """
        if not await self.is_running():
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Check existing sessions first
                resp = await client.get(
                    self._api_url(f"sessions?token={self.token}")
                )
                for s in resp.json():
                    nb = s.get("notebook", {}) or {}
                    if nb.get("path") == notebook_path:
                        return s

                if not create:
                    return None

                # No session — create one
                resp = await client.post(
                    self._api_url(f"sessions?token={self.token}"),
                    json={
                        "notebook": {"path": notebook_path},
                        "type": "notebook",
                        "kernel": {"name": "python3"},
                    },
                )
                if resp.status_code in (200, 201):
                    return resp.json()
                return None
        except Exception:
            logger.debug("get_session_for_notebook failed for %s", notebook_path, exc_info=True)
            return None

    async def get_kernel_status(self, kernel_id: str) -> dict:
        """Return kernel execution state.

        Returns dict with at least ``execution_state`` (idle/busy/starting/dead).
        """
        if not await self.is_running():
            return {"execution_state": "unknown", "connection_state": "disconnected"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    self._api_url(f"kernels/{kernel_id}?token={self.token}")
                )
                if resp.status_code == 404:
                    return {"execution_state": "dead", "connection_state": "disconnected"}
                resp.raise_for_status()
                return resp.json()
        except Exception:
            logger.debug("Failed to fetch Jupyter kernel status for %s", kernel_id, exc_info=True)
            return {"execution_state": "unknown", "connection_state": "disconnected"}

    async def interrupt_kernel(self, kernel_id: str) -> bool:
        """Send an interrupt signal to the kernel via REST API."""
        if not await self.is_running():
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self._api_url(f"kernels/{kernel_id}/interrupt?token={self.token}")
                )
                return resp.status_code == 204
        except Exception:
            logger.debug("Failed to interrupt Jupyter kernel %s", kernel_id, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Kernel management
    # ------------------------------------------------------------------

    async def list_kernels_enriched(self) -> dict:
        """List kernels with project name enrichment."""
        if not await self.is_running():
            return {"kernels": []}

        async with httpx.AsyncClient(timeout=10) as client:
            kernels_resp = await client.get(self._api_url(f"kernels?token={self.token}"))
            kernels = kernels_resp.json()
            sessions_resp = await client.get(self._api_url(f"sessions?token={self.token}"))
            sessions = sessions_resp.json()

        def trunc(name, max_len=10):
            if not name:
                return ""
            return name if len(name) <= max_len else name[:max_len] + "..."

        project_lookup = {}
        for s in sessions:
            s_notebook = s.get("notebook", {}) or {}
            spath = s_notebook.get("path", "")
            if "/" in spath:
                pid = spath.rsplit("/", 1)[0]
                if pid not in project_lookup:
                    try:
                        from app.services.project_service import project_service as _ps
                        p = await _ps.get_project(pid)
                        project_lookup[pid] = trunc(p.get("name") or pid)
                    except Exception:
                        logger.debug("Failed to resolve project name for Jupyter path %s", spath, exc_info=True)

        for k in kernels:
            k_id = k.get("id")
            nb_path = ""
            for s in sessions:
                s_info = s.get("kernel", {}) or {}
                if s_info.get("id") == k_id:
                    sn = s.get("notebook", {}) or {}
                    nb_path = sn.get("path", "")
                    k["notebook"] = sn
                    break

            if nb_path and "/" in nb_path:
                parts = nb_path.rsplit("/", 1)
                pname = project_lookup.get(parts[0], parts[0])
                k["project_name"] = pname
                k["display_name"] = f"{pname} - {parts[1]}"
            elif nb_path:
                k["display_name"] = nb_path
            else:
                k["display_name"] = k.get("name", "Untitled Kernel")

        return {"kernels": kernels}

    async def kill_project_kernels(self, project_id: str):
        """Kill all Jupyter kernels associated with a project."""
        if not await self.is_running():
            return
        async with httpx.AsyncClient(timeout=10) as client:
            sessions_resp = await client.get(self._api_url(f"sessions?token={self.token}"))
            for s in sessions_resp.json():
                nb_path = (s.get("notebook", {}) or {}).get("path", "")
                if nb_path.startswith(f"{project_id}/"):
                    kernel_id = (s.get("kernel", {}) or {}).get("id")
                    if kernel_id:
                        try:
                            await client.delete(
                                self._api_url(f"kernels/{kernel_id}?token={self.token}")
                            )
                        except Exception:
                            logger.debug("Failed to delete Jupyter kernel %s", kernel_id, exc_info=True)

    async def kill_kernel(self, kernel_id: str) -> None:
        """Kill a specific kernel via Jupyter API."""
        from app.core.exceptions import JupyterKernelError

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(
                self._api_url(f"kernels/{kernel_id}?token={self.token}")
            )
        if resp.status_code >= 400:
            raise JupyterKernelError(kernel_id, resp.status_code, resp.text)

    # ------------------------------------------------------------------
    # Code execution via WebSocket
    # ------------------------------------------------------------------

    async def execute_code(
        self, kernel_id: str, code: str, timeout: float = 60.0,
    ) -> dict:
        """Execute code on a Jupyter kernel via the WebSocket protocol.

        Returns a dict containing status, execution_count, and native
        Jupyter-compatible outputs.
        """
        ws_url = self._ws_url(f"kernels/{kernel_id}/channels?token={self.token}")

        result = {
            "status": "ok",
            "outputs": [],
            "execution_count": None,
            "error_name": None,
            "error_value": None,
            "traceback": None,
        }

        msg_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())

        execute_request = {
            "header": {
                "msg_id": msg_id,
                "msg_type": "execute_request",
                "username": "sigma",
                "session": session_id,
                "version": "5.4",
                "date": to_iso(utcnow()),
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": True,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
            "buffers": [],
            "channel": "shell",
        }

        ws = None
        try:
            ws = await websockets.connect(
                ws_url,
                ping_interval=None,
                ping_timeout=None,
                max_size=2 ** 24,
                close_timeout=5,
            )

            await ws.send(_json.dumps(execute_request))

            async def _collect():
                async for raw in ws:
                    msg = _json.loads(raw)
                    parent = msg.get("parent_header", {})
                    # Only process messages that are children of our request
                    if parent.get("msg_id") != msg_id:
                        continue

                    msg_type = msg.get("msg_type", "")
                    channel = msg.get("channel", "")
                    content = msg.get("content", {})

                    if channel == "iopub":
                        if msg_type == "stream":
                            result["outputs"].append({
                                "output_type": "stream",
                                "name": content.get("name", "stdout"),
                                "text": content.get("text", ""),
                            })
                        elif msg_type == "execute_result":
                            data = content.get("data", {})
                            output = {
                                "output_type": "execute_result",
                                "execution_count": content.get("execution_count"),
                                "data": data,
                                "metadata": content.get("metadata", {}),
                            }
                            result["outputs"].append(output)
                            result["execution_count"] = content.get("execution_count")
                        elif msg_type == "display_data":
                            result["outputs"].append({
                                "output_type": "display_data",
                                "data": content.get("data", {}),
                                "metadata": content.get("metadata", {}),
                            })
                        elif msg_type == "error":
                            result["error_name"] = content.get("ename", "")
                            result["error_value"] = content.get("evalue", "")
                            result["traceback"] = content.get("traceback", [])
                            result["status"] = "error"
                            result["outputs"].append({
                                "output_type": "error",
                                "ename": result["error_name"],
                                "evalue": result["error_value"],
                                "traceback": result["traceback"],
                            })

                    elif channel == "shell" and msg_type == "execute_reply":
                        # Final reply — execution finished
                        if content.get("execution_count") is not None:
                            result["execution_count"] = content.get("execution_count")
                        if content.get("status") == "error":
                            result["status"] = "error"
                        return

            await asyncio.wait_for(_collect(), timeout=timeout)

        except asyncio.TimeoutError:
            result["status"] = "timeout"
            result["outputs"].append({
                "output_type": "stream",
                "name": "stderr",
                "text": "[Execution timed out]\n",
            })
            # Attempt to interrupt the kernel
            if ws and ws.close_code is None:
                try:
                    interrupt = {
                        "header": {
                            "msg_id": str(uuid.uuid4()),
                            "msg_type": "interrupt_request",
                            "username": "sigma",
                            "session": session_id,
                            "version": "5.4",
                            "date": to_iso(utcnow()),
                        },
                        "parent_header": {},
                        "metadata": {},
                        "content": {},
                        "buffers": [],
                        "channel": "control",
                    }
                    await ws.send(_json.dumps(interrupt))
                except Exception:
                    logger.debug("Failed to send Jupyter interrupt for kernel %s", kernel_id, exc_info=True)
        except websockets.ConnectionClosed:
            result["status"] = "error"
            result["error_name"] = "KernelDisconnected"
            result["error_value"] = "The kernel connection was closed during execution"
            result["outputs"].append({
                "output_type": "error",
                "ename": result["error_name"],
                "evalue": result["error_value"],
                "traceback": [],
            })
        except ConnectionRefusedError:
            result["status"] = "error"
            result["error_name"] = "KernelNotFound"
            result["error_value"] = f"Cannot connect to kernel {kernel_id}. It may have died."
            result["outputs"].append({
                "output_type": "error",
                "ename": result["error_name"],
                "evalue": result["error_value"],
                "traceback": [],
            })
        except Exception as exc:
            logger.debug("Jupyter execution failed for kernel %s", kernel_id, exc_info=True)
            result["status"] = "error"
            result["error_name"] = type(exc).__name__
            result["error_value"] = str(exc)
            result["outputs"].append({
                "output_type": "error",
                "ename": result["error_name"],
                "evalue": result["error_value"],
                "traceback": [],
            })
        finally:
            if ws and ws.close_code is None:
                try:
                    await ws.close()
                except Exception:
                    logger.debug("Failed to close Jupyter websocket for kernel %s", kernel_id, exc_info=True)

        self._truncate_execution_outputs(result["outputs"])

        return result

    def _truncate_execution_outputs(self, outputs: list[dict]) -> None:
        """Bound text-heavy outputs before they are stored in the notebook."""
        remaining = _MAX_EXECUTION_OUTPUT
        for output in outputs:
            if output.get("output_type") == "stream":
                text = output.get("text", "")
                if isinstance(text, list):
                    text = "".join(str(part) for part in text)
                if len(text) > remaining:
                    output["text"] = text[:max(0, remaining)] + "\n... [truncated]"
                    remaining = 0
                else:
                    output["text"] = text
                    remaining -= len(text)
                continue

            data = output.get("data")
            if not isinstance(data, dict) or "text/plain" not in data:
                continue
            text = data["text/plain"]
            if isinstance(text, list):
                text = "".join(str(part) for part in text)
            if len(text) > remaining:
                data["text/plain"] = text[:max(0, remaining)] + "\n... [truncated]"
                remaining = 0
            else:
                data["text/plain"] = text
                remaining -= len(text)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write_config(self):
        """Write the Jupyter server config under the shared SiGMA data directory."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        config_dir = self.runtime_dir
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "jupyter_server_config.py"
        self._config_path = config_path

        config_content = f"""\
# --- nbclassic / Jupyter Server ---
c.ServerApp.ip = '0.0.0.0'
c.ServerApp.port = {self.port}
c.ServerApp.root_dir = '{self.base_dir}'
c.IdentityProvider.token = '{self.token}'
c.ServerApp.password = ''
c.ServerApp.allow_origin = '*'
c.ServerApp.disable_check_xsrf = True
c.ServerApp.open_browser = False
c.ServerApp.base_url = '/api/v1/jupyter/'
c.ServerApp.allow_remote_access = True
c.ServerApp.tornado_settings = {{
    'headers': {{
        'Content-Security-Policy': "frame-ancestors 'self' *"
    }},
}}
c.ContentsManager.allow_hidden = True
"""
        config_path.write_text(config_content, encoding="utf-8")

    async def _port_open(self) -> bool:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("localhost", self.port)) == 0

    async def _check_our_token(self) -> bool:
        """Return True if a Jupyter server on *self.port* accepts our token."""
        if not await self._port_open():
            return False
        try:
            import urllib.request
            url = self._api_url(f"status?token={self.token}")
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: urllib.request.urlopen(url, timeout=3),
            )
            return resp.status == 200
        except Exception:
            logger.debug("Jupyter status check failed on port %s", self.port, exc_info=True)
            return False

    def _kill_process(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
            logger.info("Jupyter server stopped")

    def _force_kill_port(self):
        """Kill whatever process is listening on our port."""
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("localhost", self.port))
            subprocess.run(
                ["fuser", "-k", f"{self.port}/tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            logger.debug("No Jupyter process found on port %s", self.port, exc_info=True)
        self.process = None


# ------------------------------------------------------------------
# Global instance management
# ------------------------------------------------------------------

_jupyter_instance = None


def get_jupyter():
    """Get the global Jupyter service instance.

    In the Huey worker process, ``set_jupyter`` is never called because
    ``start_jupyter`` runs inside the FastAPI web process.  This function
    falls back to lazy-initialising from the config file written by the
    web process so that tools (notebook_run_cell, notebook_read, …) can
    still reach the running Jupyter server.
    """
    global _jupyter_instance
    if _jupyter_instance is not None:
        return _jupyter_instance

    # Lazy init from config file (Huey worker path)
    try:
        config_path = settings.SIGMA_DIR / "jupyter" / "jupyter_server_config.py"
        if not config_path.exists():
            return None
        content = config_path.read_text(encoding="utf-8")
        match = re.search(r"c\.IdentityProvider\.token\s*=\s*'([^']+)'", content)
        if not match:
            match = re.search(r"c\.NotebookApp\.token\s*=\s*'([^']+)'", content)
        if not match:
            return None
        token = match.group(1)

        # Extract port from config
        port_match = re.search(r"c\.ServerApp\.port\s*=\s*(\d+)", content)
        if not port_match:
            port_match = re.search(r"c\.NotebookApp\.port\s*=\s*(\d+)", content)
        port = int(port_match.group(1)) if port_match else 8890

        svc = JupyterService(base_dir=str(settings.USERDATA_DIR), port=port)
        svc.token = token
        _jupyter_instance = svc
        return svc
    except Exception:
        logger.debug("get_jupyter lazy-init failed", exc_info=True)
        return None


def set_jupyter(svc):
    """Set the global Jupyter service instance."""
    global _jupyter_instance
    _jupyter_instance = svc
