"""
Browser Service - Manages a shared Chrome + noVNC instance.

All projects share a single global browser.
Data stored in userdata/.SiGMA/browser_data/
"""
import asyncio
import shutil
import socket
import subprocess
import time as _time
from pathlib import Path
from typing import Dict, Optional

from app.core.config import settings

from app.core.logging import get_logger
from app.core.exceptions import ServiceException
logger = get_logger(__name__)

CHROME_CDP_PORT = 9222


class BrowserService:
    """Single shared browser instance for all projects."""

    PORT_WS = 6080        # websockify (WebSocket)
    PORT_VNC = 6180       # x11vnc (RFB)
    DISPLAY = ":99"

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).resolve()
        self._procs = None          # dict name -> asyncio subprocess
        self._started = False
        self._running = False       # controls daemon loop

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _port_alive(port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                return s.connect_ex(("localhost", port)) == 0
        except OSError:
            return False

    @classmethod
    def _kill_stale(cls):
        """Kill old processes on our fixed ports / display (safe no-op when nothing is there)."""
        for p in (cls.PORT_WS, cls.PORT_VNC):
            subprocess.run(["fuser", "-k", f"{p}/tcp"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for pat in (f"Xvfb {cls.DISPLAY}",
                     f"--display={cls.DISPLAY}"):
            subprocess.run(["pkill", "-f", pat],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _time.sleep(0.3)

    # ------------------------------------------------------------------
    # Chrome launch arguments
    # ------------------------------------------------------------------
    # Defense-in-depth: disable Chrome subsystems we don't need for a
    # headless shared browser. These reduce background disk writes but are
    # NOT sufficient on their own to prevent unbounded browser_data growth
    # -- the real guard is _clean_stale_chrome_locks() below.
    _CHROME_HARDENING_FLAGS = (
        "--metrics-recording-only",
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-component-extensions-with-background-pages",
        "--disable-sync",
        "--disable-domain-reliability",
        "--disable-default-apps",
        "--disable-extensions",
    )

    @classmethod
    def _chrome_args(cls, data_dir: Path) -> list:
        """Full Chrome argv for a shared, sandbox-free, telemetry-light session."""
        return [
            f"--display={cls.DISPLAY}",
            f"--user-data-dir={data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            *cls._CHROME_HARDENING_FLAGS,
            f"--remote-debugging-port={CHROME_CDP_PORT}",
            "--window-size=1920,1080",
            "about:blank",
        ]

    @staticmethod
    def _clean_stale_chrome_locks(data_dir: Path) -> None:
        """Remove Chrome's Singleton* lock files if present.

        Chrome writes ``SingletonLock -> hostname-PID`` (plus Cookie/Socket)
        on startup to claim the profile. In containers the hostname changes
        on every restart, so the lock goes stale. Chrome then refuses to
        start ("profile appears to be in use by another Chrome process"),
        exits immediately, and the daemon relaunches it in a tight loop --
        each crash dumps a 4MB BrowserMetrics .pma that accumulates without
        bound.

        Safe to call unconditionally: _kill_stale() has already terminated
        any live Chrome before we reach this point, so any remaining lock
        is by definition stale.
        """
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = data_dir / name
            try:
                if p.is_symlink() or p.exists():
                    p.unlink()
                    logger.info("Removed stale Chrome lock %s", p)
            except OSError:
                logger.debug("Failed to remove stale Chrome lock %s", p, exc_info=True)

    @staticmethod
    def _find_chrome() -> Optional[str]:
        for c in [settings.CHROME_BIN,
                    "google-chrome", "google-chrome-stable"]:
            if not c:
                continue
            p = shutil.which(c)
            if p and "/snap/" not in p:
                return p
        return None

    # ------------------------------------------------------------------
    # is-running check (with post-restart recovery)
    # ------------------------------------------------------------------
    async def _is_running(self) -> bool:
        # 1. use stored process objects if available
        if self._procs is not None:
            ws = self._procs.get("websockify")
            if ws is not None and ws.returncode is None:
                return True
            # stale – fall through
            self._procs = None
            self._started = False

        # 2. processes survived a backend restart – adopt them
        if self._port_alive(self.PORT_WS):
            self._started = True
            return True

        return False

    @property
    def vnc_port(self) -> Optional[int]:
        return self.PORT_WS if self._started else None

    @staticmethod
    def _url_str() -> str:
        return f"http://localhost:{BrowserService.PORT_WS}/vnc.html?autoconnect=true"

    # ------------------------------------------------------------------
    # Public API for the shared browser instance.
    # ------------------------------------------------------------------
    async def start(self) -> Dict:
        if await self._is_running():
            return {"status": "running", "url": self._url_str()}
        return await self._do_start()

    async def stop(self) -> Dict:
        await self.stop_all()
        return {"status": "stopped"}

    async def get_status(self) -> Dict:
        if await self._is_running():
            return {"status": "running", "url": self._url_str()}
        return {"status": "stopped"}

    async def stop_all(self):
        if self._procs is not None:
            for name in ("websockify", "x11vnc", "chrome", "xvfb"):
                proc = self._procs.get(name)
                if proc:
                    try:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            logger.debug("Failed to kill browser process %s", name, exc_info=True)
            self._procs = None
        elif self._started:
            self._kill_stale()
        self._started = False

    # ------------------------------------------------------------------
    # Daemon: automatically relaunch Chrome when it dies
    # ------------------------------------------------------------------
    async def _relaunch_chrome(self) -> Optional[asyncio.subprocess.Process]:
        """Spawn a single Chrome process. Returns the subprocess or None on failure."""
        chrome_cmd = self._find_chrome()
        if not chrome_cmd:
            logger.error("No Chrome binary found, cannot relaunch.")
            return None

        data_dir = settings.SIGMA_DIR / "browser_data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._clean_stale_chrome_locks(data_dir)

        proc = await asyncio.create_subprocess_exec(
            chrome_cmd,
            *self._chrome_args(data_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(1.5)
        return proc

    async def _chrome_daemon(self):
        """Background loop that watches for Chrome's demise and relaunches it."""
        logger.info("Chrome daemon started.")
        # Wait for websockify to be ready before monitoring Chrome
        for _ in range(30):  # up to 15 seconds
            if self._port_alive(self.PORT_WS):
                break
            await asyncio.sleep(0.5)

        while self._running:
            chrome_proc = self._procs.get("chrome") if self._procs else None
            if chrome_proc is None or chrome_proc.returncode is None:
                await asyncio.sleep(2)
                continue

            # Chrome has exited — check _running before relaunching
            if not self._running:
                break
            logger.warning(f"Chrome exited with code {chrome_proc.returncode}, relaunching...")
            new_chrome = await self._relaunch_chrome()
            if new_chrome and self._procs:
                self._procs["chrome"] = new_chrome

        logger.info("Chrome daemon stopped.")

    # ------------------------------------------------------------------
    # Application lifecycle hooks (called once each from FastAPI startup /
    # shutdown via ``app.core.lifecycle``). Distinct from per-project
    # ``start(project_id)`` / ``stop(project_id)`` which run on demand.
    # ------------------------------------------------------------------
    async def on_startup(self) -> Dict:
        """Launch the full browser stack and spawn the Chrome daemon.

        Called once during application startup. Returns the start result
        dict (``{"status": "running", ...}``) for logging.
        """
        self._running = True
        result = await self._do_start()
        if result.get("status") == "running":
            asyncio.create_task(self._chrome_daemon())
        return result

    async def on_shutdown(self):
        """Stop the daemon and terminate every spawned browser process.

        Called once during application shutdown. Idempotent — safe to call
        even if ``on_startup`` never ran or the stack already exited.
        """
        self._running = False
        # Give daemon a moment to notice
        await asyncio.sleep(0.2)
        # Stop all
        if self._procs is not None:
            for name in ("websockify", "x11vnc", "chrome", "xvfb"):
                proc = self._procs.get(name)
                if proc:
                    try:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except Exception:
                            logger.debug("Failed to kill browser process %s", name, exc_info=True)
                    except Exception:
                        logger.debug("Failed to stop browser process %s", name, exc_info=True)
            self._procs = None
        elif self._started:
            self._kill_stale()
        self._started = False

    # ------------------------------------------------------------------
    # internal start
    # ------------------------------------------------------------------
    async def _do_start(self) -> Dict:
        # clean stale first
        self._kill_stale()

        data_dir = settings.SIGMA_DIR / "browser_data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._clean_stale_chrome_locks(data_dir)

        try:
            # 1. Xvfb
            xvfb = await asyncio.create_subprocess_exec(
                "Xvfb", self.DISPLAY, "-screen", "0", "1920x1080x24", "-ac",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.sleep(0.5)

            # 2. Chrome
            chrome_cmd = self._find_chrome()
            if not chrome_cmd:
                raise FileNotFoundError("No Chrome/Chromium binary found.")

            chrome = await asyncio.create_subprocess_exec(
                chrome_cmd,
                *self._chrome_args(data_dir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.sleep(1.5)  # give Chrome time to render

            # 3. x11vnc
            x11vnc_bin = shutil.which("x11vnc")
            if not x11vnc_bin:
                raise FileNotFoundError("x11vnc not found.")

            x11vnc = await asyncio.create_subprocess_exec(
                x11vnc_bin, "-display", self.DISPLAY, "-rfbport", str(self.PORT_VNC),
                "-forever", "-shared", "-nopw", "-noshm",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.sleep(0.5)

            # 4. websockify
            ws_bin = shutil.which("websockify")
            if not ws_bin:
                raise FileNotFoundError("websockify not found.")

            websockify = await asyncio.create_subprocess_exec(
                ws_bin, str(self.PORT_WS), f"localhost:{self.PORT_VNC}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.sleep(0.5)

            self._procs = {
                "xvfb": xvfb,
                "chrome": chrome,
                "x11vnc": x11vnc,
                "websockify": websockify,
            }
            self._started = True
            logger.info(f"Shared browser started on {self.PORT_WS} (display {self.DISPLAY})")
            return {"status": "running", "url": self._url_str()}

        except Exception as e:
            logger.error(f"Failed to start shared browser: {e}")
            await self._stop_by_cleanup()
            raise ServiceException(str(e), code="BROWSER_START_FAILED", status_code=500)

    async def _stop_by_cleanup(self):
        self._kill_stale()
        self._procs = None
        self._started = False


# ------------------------------------------------------------------
browser_service: Optional[BrowserService] = None


def get_browser_service() -> BrowserService:
    global browser_service
    if browser_service is None:
        browser_service = BrowserService(base_dir=str(settings.SIGMA_DIR))
    return browser_service
