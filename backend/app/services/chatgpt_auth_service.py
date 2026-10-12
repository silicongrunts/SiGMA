"""ChatGPT subscription (Codex OAuth) authentication service.

Owns the full subscription-account lifecycle for SiGMA:

* the canonical token file (under ``userdata/.SiGMA/chatgpt``) and the one-way
  derived mirror that litellm's chatgpt Authenticator reads (its location is
  pinned via ``CHATGPT_TOKEN_DIR`` in ``app.core.config``);
* the interactive login: PKCE + authorization-code OAuth where the callback
  arrives either through a temporary loopback listener on 127.0.0.1:1455
  (the embedded Chrome shares the container loopback, so no port mapping is
  needed) or through a callback URL the user pastes manually;
* importing an auth file exported by the official Codex CLI;
* the proactive refresh chain — SiGMA is the *only* refresher (the provider's
  refresh token rotates on every use, so two refreshers would kill each
  other's chain); litellm's own lazy refresh stays dormant behind the 24h
  margin kept here;
* quota, credit-balance, and rate-limit reset-credit lookups plus reset
  redemption against the Codex usage endpoints.

Security invariants: token files are written atomically (mkstemp → rename)
and therefore carry 0600 permissions; tokens are never logged; terminal
refresh failures (``invalid_grant`` / ``refresh_token_reused``) are
irreversible and can only be answered with a fresh interactive login.
"""

import asyncio
import base64
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncGenerator
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.atomic_file import atomic_write_json
from app.core.config import (
    CHATGPT_AUTH_FILE,
    CHATGPT_ORIGINATOR,
    CHATGPT_TOKEN_DIR,
    CHATGPT_USER_AGENT,
)
from app.core.exceptions import ChatGPTAuthError, ConfigurationError
from app.core.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# OAuth / endpoint constants (same first-party client as the Codex CLI)
# ---------------------------------------------------------------------------

OAUTH_AUTHORIZATION_URL = "https://auth.openai.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CHATGPT_API_BASE = "https://chatgpt.com/backend-api"
CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_PORT = 1455
OAUTH_REDIRECT_URI = f"http://localhost:{OAUTH_REDIRECT_PORT}/auth/callback"
OAUTH_SCOPES = "openid email profile offline_access"

LOGIN_TIMEOUT_SECONDS = 600          # whole interactive login window
CALLBACK_EXCHANGE_TIMEOUT = 60       # paste path: wait for the flow to finish
REFRESH_MARGIN_SECONDS = 24 * 3600   # proactive refresh lead
REFRESH_ATTEMPTS = 3                 # transient-failure attempts, like the official CLI
MAINTENANCE_INTERVAL_SECONDS = 6 * 3600
TERMINAL_REFRESH_ERRORS = {"invalid_grant", "refresh_token_reused"}
TRANSIENT_REFRESH_STATUSES = {408, 429, 500, 502, 503, 504}

# Reserved BrowserManager owner for the OAuth tab; closed via
# close_owned_tabs(project_id=...) so the login flow fully owns its tab.
OAUTH_TAB_OWNER = "__chatgpt_oauth__"

_MIRROR_FIELDS = ("access_token", "refresh_token", "id_token", "expires_at", "account_id")


# ---------------------------------------------------------------------------
# Small value types
# ---------------------------------------------------------------------------

@dataclass
class _CallbackResult:
    code: str


@dataclass
class _CallbackError:
    message: str


@dataclass
class _LoginSession:
    """One interactive login attempt (browser tab + listener + paste)."""

    open_browser: bool
    verifier: str
    state: str
    authorize_url: str
    created_at: float = field(default_factory=time.monotonic)
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    result: "asyncio.Future[_CallbackResult | _CallbackError]" = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    done: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: dict = field(default_factory=dict)
    task: asyncio.Task | None = None

    def deliver(self, value: "_CallbackResult | _CallbackError") -> bool:
        """Deliver the first callback outcome; later deliveries are ignored."""
        if self.result.done():
            return False
        self.result.set_result(value)
        return True


def _decode_jwt_claims(token: str) -> dict:
    """Return the unverified JWT payload claims (used for metadata only)."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    except Exception:
        return {}


def _plan_from_claims(claims: dict, auth_claims: dict) -> str:
    """Plan tier from token claims — Codex tokens nest it in the auth claim."""
    return auth_claims.get("chatgpt_plan_type") or claims.get("chatgpt_plan_type") or ""


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(48)[:64]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii")
    return verifier, challenge.rstrip("=")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ChatGPTAuthService:
    def __init__(self) -> None:
        self._refresh_lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self._login: _LoginSession | None = None
        # Set once a refresh hits a terminal error; only a fresh interactive
        # login (or import) clears it.
        self._refresh_terminal = False
        self._maintenance_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Token records
    # ------------------------------------------------------------------

    def _read_canonical(self) -> dict | None:
        try:
            data = json.loads(CHATGPT_AUTH_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) and data.get("access_token") else None

    def _persist(self, record: dict) -> None:
        canonical = {k: record[k] for k in (
            "access_token", "refresh_token", "id_token", "expires_at",
            "account_id", "email", "plan_type", "updated_at",
        ) if record.get(k)}
        CHATGPT_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        CHATGPT_TOKEN_DIR.chmod(0o700)
        atomic_write_json(CHATGPT_AUTH_FILE, canonical)
        CHATGPT_AUTH_FILE.chmod(0o600)

        # One-way derived mirror: only the fields litellm's Authenticator
        # understands. litellm never writes the canonical file.
        mirror_path = CHATGPT_TOKEN_DIR / "auth.json"
        atomic_write_json(mirror_path, {k: record[k] for k in _MIRROR_FIELDS if record.get(k)})
        mirror_path.chmod(0o600)

    def _build_record(self, tokens: dict) -> dict:
        """Normalize an OAuth token response into the canonical record."""
        id_token = tokens.get("id_token") or ""
        access_token = tokens.get("access_token") or ""
        expires_in = tokens.get("expires_in")
        now = int(time.time())
        if not isinstance(expires_in, (int, float)) or expires_in <= 0:
            expires_in = _decode_jwt_claims(access_token).get("exp") or now + 3600
            expires_in = max(int(expires_in) - now, 60)
        claims = _decode_jwt_claims(id_token) if id_token else _decode_jwt_claims(access_token)
        auth_claims = claims.get("https://api.openai.com/auth") or {}
        account_id = (
            tokens.get("account_id")
            or auth_claims.get("chatgpt_account_id")
            or claims.get("chatgpt_account_id")
            or ""
        )
        return {
            "access_token": access_token,
            "refresh_token": tokens.get("refresh_token") or "",
            "id_token": id_token,
            "expires_at": now + int(expires_in),
            "account_id": account_id,
            "email": claims.get("email") or "",
            "plan_type": _plan_from_claims(claims, auth_claims),
            "updated_at": now,
        }

    # ------------------------------------------------------------------
    # Public status / guards
    # ------------------------------------------------------------------

    def status(self) -> dict:
        session = self._login
        payload: dict = {"login_pending": session is not None}
        if session is not None:
            # Present while a login is in flight so a page refresh (which
            # drops the SSE stream) can restore the running-login view: the
            # URL to authorize at and whether the embedded browser owns it.
            payload["login_authorize_url"] = session.authorize_url
            payload["login_open_browser"] = session.open_browser

        record = self._read_canonical()
        if not record:
            payload["state"] = "logged_out"
            return payload
        remaining = int(float(record.get("expires_at") or 0) - time.time())
        state = "active" if remaining > 0 else "expired"
        if self._refresh_terminal or (remaining <= 0 and not record.get("refresh_token")):
            state = "needs_relogin"
        plan_type = record.get("plan_type") or ""
        if not plan_type:
            # Records persisted before plan claims were read from the nested
            # auth claim carry no plan; re-derive it from the stored token
            # until the next proactive refresh rewrites the record.
            token_claims = _decode_jwt_claims(
                record.get("id_token") or record.get("access_token") or ""
            )
            plan_type = _plan_from_claims(
                token_claims, token_claims.get("https://api.openai.com/auth") or {}
            )
        payload.update({
            "state": state,
            "email": record.get("email") or "",
            "plan_type": plan_type,
            "account_id": record.get("account_id") or "",
            "expires_at": record.get("expires_at"),
            "remaining_seconds": max(remaining, 0),
        })
        return payload

    async def ensure_ready(self) -> None:
        """Fail fast at the LLM boundary when the subscription is unusable.

        Raises ``ConfigurationError`` (non-retryable in LLMService) rather
        than ``ChatGPTAuthError`` so a call with provider=chatgpt but no
        valid login surfaces as a configuration problem — and, critically,
        never falls through to litellm, whose own fallback would silently
        start an interactive device-code login inside the request path.
        """
        try:
            await self.get_access_token()
        except ChatGPTAuthError as exc:
            raise ConfigurationError(str(exc)) from exc

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing proactively when the
        remaining lifetime drops below ``REFRESH_MARGIN_SECONDS``."""
        async with self._refresh_lock:
            record = self._read_canonical()
            if not record:
                raise ChatGPTAuthError("ChatGPT subscription is not signed in.")
            remaining = float(record.get("expires_at") or 0) - time.time()
            if remaining < REFRESH_MARGIN_SECONDS:
                record = await self._refresh_locked(record)
            return record["access_token"]

    # ------------------------------------------------------------------
    # Refresh chain (SiGMA is the sole refresher)
    # ------------------------------------------------------------------

    async def _refresh_locked(self, record: dict) -> dict:
        """Refresh the token pair and return the persisted record.

        Same request shape and transient-failure tolerance as the official
        CLI (form-encoded with ``scope``, three attempts with linear
        backoff). ``invalid_grant`` / ``refresh_token_reused`` stay
        immediately terminal — retrying a dead refresh token is pointless.
        """
        refresh_token = record.get("refresh_token") or ""
        if not refresh_token:
            self._refresh_terminal = True
            raise ChatGPTAuthError(
                "ChatGPT session has no refresh token — sign in again."
            )
        last_error: ChatGPTAuthError | None = None
        async with httpx.AsyncClient(timeout=15.0) as client:
            for attempt in range(1, REFRESH_ATTEMPTS + 1):
                try:
                    resp = await client.post(
                        OAUTH_TOKEN_URL,
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_token,
                            "client_id": CHATGPT_CLIENT_ID,
                            "scope": "openid profile email",
                        },
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Accept": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_error = ChatGPTAuthError(
                        f"Could not reach the ChatGPT token endpoint: {exc}"
                    )
                else:
                    if resp.status_code == 200:
                        return self._persist_refreshed(record, resp)
                    error = self._oauth_error(resp)
                    if resp.status_code == 400 and error in TERMINAL_REFRESH_ERRORS:
                        self._refresh_terminal = True
                        logger.warning("ChatGPT refresh chain is terminal (%s)", error)
                        raise ChatGPTAuthError(
                            "ChatGPT refresh token is no longer valid — sign in again."
                        )
                    if resp.status_code not in TRANSIENT_REFRESH_STATUSES:
                        raise ChatGPTAuthError(
                            f"ChatGPT token refresh failed (HTTP {resp.status_code})."
                        )
                    last_error = ChatGPTAuthError(
                        f"ChatGPT token refresh failed (HTTP {resp.status_code})."
                    )
                if attempt < REFRESH_ATTEMPTS:
                    await asyncio.sleep(attempt)
        assert last_error is not None
        raise last_error

    def _persist_refreshed(self, record: dict, resp: httpx.Response) -> dict:
        """Normalize a successful refresh response and persist the record."""
        payload = resp.json()
        tokens = {
            "access_token": payload.get("access_token") or record["access_token"],
            # Refresh-token rotation: keep the old value when the response
            # does not carry a replacement.
            "refresh_token": payload.get("refresh_token") or record["refresh_token"],
            "id_token": payload.get("id_token") or record.get("id_token") or "",
            "expires_in": payload.get("expires_in"),
            "account_id": record.get("account_id") or "",
        }
        updated = self._build_record(tokens)
        # Metadata fields may vanish when the refreshed id_token omits them.
        updated["email"] = updated["email"] or record.get("email") or ""
        updated["plan_type"] = updated["plan_type"] or record.get("plan_type") or ""
        self._persist(updated)
        self._refresh_terminal = False
        logger.info(
            "ChatGPT token refreshed (expires in %.1fh, email=%s)",
            (updated["expires_at"] - time.time()) / 3600, updated["email"] or "unknown",
        )
        return updated

    @staticmethod
    def _oauth_error(resp: httpx.Response) -> str:
        try:
            return str(resp.json().get("error") or "")
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Interactive login
    # ------------------------------------------------------------------

    async def start_login(self, *, open_browser: bool = True) -> _LoginSession:
        """Begin an interactive login and run the flow in a service-owned
        task (it survives SSE client disconnects and page refreshes).

        With ``open_browser`` the authorize page opens in the embedded Chrome
        and its picture streams to the login overlay. Without it the user
        opens ``authorize_url`` (surfaced via ``status()``) in their own
        browser — e.g. when sign-in requires a hardware security key. Either
        way the callback arrives through the loopback listener or the paste
        endpoint.
        """
        async with self._login_lock:
            if self._login is not None:
                raise ChatGPTAuthError("A ChatGPT login is already in progress.")
            verifier, challenge = _pkce_pair()
            state = secrets.token_urlsafe(24)
            query = {
                "response_type": "code",
                "client_id": CHATGPT_CLIENT_ID,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "scope": OAUTH_SCOPES,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
            session = _LoginSession(
                open_browser=open_browser,
                verifier=verifier,
                state=state,
                authorize_url=f"{OAUTH_AUTHORIZATION_URL}?{httpx.QueryParams(query)}",
            )
            self._login = session
            session.task = asyncio.create_task(self._run_login_flow(session))
            return session

    async def cancel_login(self) -> None:
        """Cancel the in-flight login session, if any, and detach it.

        The session is detached under the lock — not by the flow task's
        finally — because a cancel arriving before the task's first step
        would otherwise leave a ghost ``login_pending`` behind.
        """
        async with self._login_lock:
            session, self._login = self._login, None
        if session is None:
            return
        if session.task is not None:
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)
        session.done.set()

    async def stream_login_events(self) -> AsyncGenerator[tuple[str, dict], None]:
        """Re-play events of the active login until it finishes."""
        session = self._login
        if session is None:
            raise ChatGPTAuthError("No ChatGPT login is in progress.")
        while True:
            event = await session.events.get()
            yield event
            if event[0] == "login_done":
                break

    async def complete_callback_url(self, url: str) -> dict:
        """Paste-path callback delivery for the pending login session."""
        async with self._login_lock:
            session = self._login
        if session is None:
            raise ChatGPTAuthError("No ChatGPT login is in progress — start one first.")
        parsed = urlparse(url.strip())
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if query.get("error"):
            raise ChatGPTAuthError(f"Login was rejected: {query.get('error_description') or query['error']}")
        code = query.get("code") or ""
        if not code:
            raise ChatGPTAuthError("The pasted URL contains no authorization code.")
        if query.get("state") != session.state:
            raise ChatGPTAuthError(
                "This callback URL belongs to a different login attempt."
            )
        if not session.deliver(_CallbackResult(code=code)):
            raise ChatGPTAuthError("A callback was already delivered for this login.")
        try:
            await asyncio.wait_for(session.done.wait(), timeout=CALLBACK_EXCHANGE_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise ChatGPTAuthError("Timed out waiting for the login to complete.") from exc
        outcome = dict(session.outcome)
        if outcome.get("status") != "success":
            raise ChatGPTAuthError(outcome.get("message") or "Login did not complete.")
        return outcome

    def _emit(self, session: _LoginSession, event: str, data: dict | None = None) -> None:
        session.events.put_nowait((event, data or {}))

    async def _run_login_flow(self, session: _LoginSession) -> None:
        from app.agents.tools.browser_manager import get_browser_manager
        from app.agents.tools.browser_tab_reaper import get_tab_reaper
        from app.agents.tools.browser_thread import dispatch as browser_dispatch

        reaper = get_tab_reaper()
        reaper.suspend()
        server: asyncio.AbstractServer | None = None
        try:
            self._emit(session, "login_start", {"authorize_url": session.authorize_url})
            if session.open_browser:
                # The shared Chrome may be down (it only auto-starts for the
                # Explore view); bring the stack up so the OAuth tab can open.
                try:
                    from app.services.browser_service import get_browser_service
                    browser_svc = get_browser_service()
                    if (await browser_svc.get_status()).get("status") != "running":
                        await browser_svc.start()
                except Exception as exc:
                    logger.warning("ChatGPT login could not start the shared browser: %s", exc)
                # The OAuth tab is best-effort: when Chrome is unavailable
                # (host dev without the embedded browser) the user can still
                # open the authorize URL elsewhere and paste the callback.
                try:
                    tab = await browser_dispatch(get_browser_manager().create_page(
                        session.authorize_url, project_id=OAUTH_TAB_OWNER,
                    ))
                    self._emit(session, "login_browser", {
                        "opened": True, "url": session.authorize_url, "tab_id": tab.get("id"),
                    })
                except Exception as exc:
                    logger.warning("ChatGPT login tab could not be opened: %s", exc)
                    self._emit(session, "login_browser", {
                        "opened": False, "url": session.authorize_url, "message": str(exc),
                    })

            server = await self._start_listener(session)
            if server is None:
                logger.warning(
                    "ChatGPT callback port %d unavailable — paste-mode only",
                    OAUTH_REDIRECT_PORT,
                )
                self._emit(session, "login_listener", {"listening": False})

            while True:
                remaining = LOGIN_TIMEOUT_SECONDS - (time.monotonic() - session.created_at)
                result = await asyncio.wait_for(session.result, timeout=max(remaining, 1.0))
                if isinstance(result, _CallbackError):
                    raise ChatGPTAuthError(result.message)
                self._emit(session, "login_callback", {})
                record = await self._exchange_code(result.code, session.verifier)
                self._refresh_terminal = False
                self._emit(session, "login_success", {
                    "email": record.get("email") or "",
                    "plan_type": record.get("plan_type") or "",
                })
                session.outcome = {
                    "status": "success",
                    "email": record.get("email") or "",
                    "plan_type": record.get("plan_type") or "",
                }
                break
        except asyncio.CancelledError:
            session.outcome = {"status": "cancelled"}
            self._emit(session, "login_error", {"message": "Login cancelled.", "error_type": "cancelled"})
        except asyncio.TimeoutError:
            session.outcome = {"status": "error", "message": "Login timed out."}
            self._emit(session, "login_error", {
                "message": "Login timed out — start again.", "error_type": "timeout",
            })
        except ChatGPTAuthError as exc:
            logger.info("ChatGPT login failed: %s", exc.message)
            session.outcome = {"status": "error", "message": exc.message}
            self._emit(session, "login_error", {"message": exc.message, "error_type": "auth"})
        except Exception as exc:  # noqa: BLE001 — surfaced to the UI, then contained
            logger.warning("ChatGPT login crashed: %s", exc, exc_info=True)
            session.outcome = {"status": "error", "message": str(exc)}
            self._emit(session, "login_error", {"message": str(exc), "error_type": "internal"})
        finally:
            if server is not None:
                server.close()
                try:
                    await server.wait_closed()
                except Exception:
                    logger.debug("ChatGPT callback server close failed", exc_info=True)
            try:
                await browser_dispatch(get_browser_manager().close_owned_tabs(
                    project_id=OAUTH_TAB_OWNER,
                ))
            except Exception:
                logger.debug("ChatGPT OAuth tab close failed", exc_info=True)
            reaper.resume()
            async with self._login_lock:
                if self._login is session:
                    self._login = None
            session.done.set()
            self._emit(session, "login_done", {})

    async def _start_listener(self, session: _LoginSession) -> asyncio.AbstractServer:
        """Bind the loopback callback listener, or return None when the port
        is unavailable (paste-mode still works without it)."""
        try:
            return await asyncio.start_server(
                lambda r, w: self._handle_callback(r, w, session),
                host="127.0.0.1", port=OAUTH_REDIRECT_PORT,
            )
        except OSError:
            return None

    async def _handle_callback(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        session: _LoginSession,
    ) -> None:
        """One HTTP request from the OAuth redirect; answers with a static
        page and hands the outcome to the login flow."""
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        try:
            request_line = raw.split(b"\r\n", 1)[0].decode("latin-1")
            parts = request_line.split(" ")
            target = parts[1] if len(parts) > 1 else ""
            query = {k: v[0] for k, v in parse_qs(urlparse(target).query).items()}
        except Exception:
            query = {}

        if query.get("error"):
            session.deliver(_CallbackError(
                message=query.get("error_description") or str(query["error"]),
            ))
            body = "<h2>Sign-in failed</h2><p>You can close this tab.</p>"
        elif query.get("code") and query.get("state") == session.state:
            delivered = session.deliver(_CallbackResult(code=query["code"]))
            body = (
                "<h2>Sign-in received</h2>"
                "<p>You can close this tab and return to SiGMA.</p>"
                if delivered else
                "<h2>Already processed</h2><p>You can close this tab.</p>"
            )
        else:
            body = "<h2>Unexpected callback</h2><p>You can close this tab.</p>"

        page = (
            "<!doctype html><html><head><meta charset='utf-8'><title>SiGMA</title></head>"
            "<body style=\"font-family:sans-serif;max-width:28rem;margin:4rem auto;"
            "text-align:center;color:#111\">"
            f"<div style='font-size:2rem'>🧠</div>{body}</body></html>"
        )
        try:
            writer.write(
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(page.encode('utf-8'))}\r\nConnection: close\r\n\r\n"
                f"{page}".encode("utf-8")
            )
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def _exchange_code(self, code: str, verifier: str) -> dict:
        """Exchange the authorization code and persist the token record."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    OAUTH_TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": OAUTH_REDIRECT_URI,
                        "client_id": CHATGPT_CLIENT_ID,
                        "code_verifier": verifier,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            raise ChatGPTAuthError(f"Could not reach the ChatGPT token endpoint: {exc}") from exc
        if resp.status_code != 200:
            error = self._oauth_error(resp)
            raise ChatGPTAuthError(
                f"Authorization code exchange failed (HTTP {resp.status_code}"
                + (f", {error}" if error else "") + ")."
            )
        record = self._build_record(resp.json())
        if not record["access_token"] or not record["refresh_token"]:
            raise ChatGPTAuthError("Token response is missing the access/refresh token.")
        self._persist(record)
        logger.info("ChatGPT login persisted (email=%s, plan=%s)", record["email"], record["plan_type"])
        return record

    # ------------------------------------------------------------------
    # Import (official Codex CLI auth.json)
    # ------------------------------------------------------------------

    async def import_auth_json(self, content: str) -> dict:
        """Import a token file from the official Codex CLI (nested ``tokens``
        structure) or an already-flat record, then validate it.

        An import supersedes an interactive login in flight: the pending
        login task is cancelled before persisting, so a concurrently
        completing code exchange cannot overwrite the imported record.
        """
        record = self._parse_import(content)
        await self.cancel_login()
        self._persist(record)
        self._refresh_terminal = False
        logger.info("ChatGPT auth file imported (email=%s)", record["email"])
        return {
            "status": "success",
            "email": record["email"],
            "plan_type": record["plan_type"],
            "expires_at": record["expires_at"],
        }

    @staticmethod
    def _parse_import(content: str) -> dict:
        """Validate importable content and normalize it into a token record."""
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ChatGPTAuthError("The file is not valid JSON.") from exc
        if not isinstance(data, dict):
            raise ChatGPTAuthError("The file must contain a JSON object.")

        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else data
        merged = {
            "access_token": tokens.get("access_token") or "",
            "refresh_token": tokens.get("refresh_token") or "",
            "id_token": tokens.get("id_token") or "",
            "account_id": tokens.get("account_id") or data.get("account_id") or "",
        }
        if not merged["access_token"] or not merged["refresh_token"]:
            raise ChatGPTAuthError(
                "The file must contain both an access token and a refresh token."
            )
        expires_at = tokens.get("expires_at") or data.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at > 10**12:
            # Millisecond epoch (JS exports) → seconds.
            expires_at = expires_at / 1000
        claims = _decode_jwt_claims(merged["id_token"] or merged["access_token"])
        auth_claims = claims.get("https://api.openai.com/auth") or {}
        if not merged["account_id"]:
            merged["account_id"] = (
                auth_claims.get("chatgpt_account_id") or claims.get("chatgpt_account_id") or ""
            )
        if not isinstance(expires_at, (int, float)) or expires_at <= 0:
            expires_at = claims.get("exp")
        if not isinstance(expires_at, (int, float)) or expires_at <= 0:
            raise ChatGPTAuthError("Cannot determine the token expiry from this file.")

        return {
            **merged,
            "expires_at": int(expires_at),
            "email": claims.get("email") or "",
            "plan_type": _plan_from_claims(claims, auth_claims),
            "updated_at": int(time.time()),
        }

    # ------------------------------------------------------------------
    # Logout
    # ------------------------------------------------------------------

    async def logout(self) -> dict:
        await self.cancel_login()
        for path in (CHATGPT_AUTH_FILE, CHATGPT_TOKEN_DIR / "auth.json"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ChatGPTAuthError(f"Could not remove the token file: {exc}") from exc
        self._refresh_terminal = False
        logger.info("ChatGPT subscription signed out")
        return {"status": "success"}

    # ------------------------------------------------------------------
    # Quota (wham/usage)
    # ------------------------------------------------------------------

    async def fetch_usage(self) -> dict:
        return await self._wham_request("GET", "/wham/usage", action="usage lookup")

    async def fetch_reset_credits(self) -> dict:
        """Available rate-limit reset credits ("reset cards").

        ``credits`` rows are detail samples — the authoritative total is
        ``available_count`` (the backend caps the row list), matching the
        official client's reading.
        """
        return await self._wham_request(
            "GET", "/wham/rate-limit-reset-credits", action="reset-credit lookup"
        )

    async def consume_reset_credit(self, credit_id: str | None = None) -> dict:
        """Redeem one reset credit to clear the current rate-limit window.

        ``redeem_request_id`` is a client-generated idempotency key, exactly
        like the official CLI: the server deduplicates a re-delivery of one
        identical request, while each new call mints a fresh key and redeems
        its own credit.
        """
        body = {"redeem_request_id": str(uuid.uuid4())}
        if credit_id:
            body["credit_id"] = credit_id
        return await self._wham_request(
            "POST",
            "/wham/rate-limit-reset-credits/consume",
            json_body=body,
            action="reset-credit redemption",
        )

    async def _wham_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        action: str,
    ) -> dict:
        token = await self.get_access_token()
        record = self._read_canonical() or {}
        headers = {
            "Authorization": f"Bearer {token}",
            "originator": CHATGPT_ORIGINATOR,
            "User-Agent": CHATGPT_USER_AGENT,
        }
        if record.get("account_id"):
            headers["chatgpt-account-id"] = record["account_id"]
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.request(
                    method, f"{CHATGPT_API_BASE}{path}", headers=headers, json=json_body
                )
        except httpx.HTTPError as exc:
            raise ChatGPTAuthError(f"Could not reach the Codex backend ({action}): {exc}") from exc
        if resp.status_code == 401:
            raise ChatGPTAuthError("The session token was rejected — sign in again.")
        if resp.status_code != 200:
            raise ChatGPTAuthError(f"{action.capitalize()} failed (HTTP {resp.status_code}).")
        return resp.json()

    # ------------------------------------------------------------------
    # Maintenance loop (idle-keepalive refresh chain)
    # ------------------------------------------------------------------

    async def startup_check(self) -> None:
        """Refresh at startup when the token is inside the margin. Never
        raises — a failed refresh must not block the application."""
        try:
            record = self._read_canonical()
            if record and not self._refresh_terminal:
                remaining = float(record.get("expires_at") or 0) - time.time()
                if remaining < REFRESH_MARGIN_SECONDS:
                    await self.get_access_token()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("ChatGPT startup token check failed: %s", exc)

    def start_maintenance(self) -> None:
        if self._maintenance_task is None or self._maintenance_task.done():
            self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def stop_maintenance(self) -> None:
        task, self._maintenance_task = self._maintenance_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _maintenance_loop(self) -> None:
        """Periodically keep the refresh chain alive even when SiGMA idles.

        Long-lived refresh chains that are never exercised can be rejected by
        the provider; refreshing while still inside the 24h margin keeps the
        chain warm without risking its rotation.
        """
        while True:
            await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)
            try:
                record = self._read_canonical()
                if record and not self._refresh_terminal:
                    remaining = float(record.get("expires_at") or 0) - time.time()
                    if remaining < REFRESH_MARGIN_SECONDS:
                        await self.get_access_token()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("ChatGPT maintenance refresh failed: %s", exc)


chatgpt_auth_service = ChatGPTAuthService()
