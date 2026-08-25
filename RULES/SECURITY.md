# Security Rules

Security-sensitive code is centralized, testable, and boring. SiGMA is
single-user and local, but inputs can still be invalid: user paths, files,
configuration, LLM APIs, and model-generated tool inputs. Do not duplicate
security decisions in components, tools, or routes.

## Filesystem And Path Safety

- Shared path helpers for containment checks; never string prefix checks.
  Resolve paths before permission decisions.
- Consider symlink behavior when resolving user-supplied paths.
- Uploaded filenames are sanitized by the shared filename sanitizer;
  reject path separators, traversal, empty names, and hidden names where a
  plain filename is expected.

## Bounded Agent Filesystem Access

Agent-driven reads are unrestricted by design (any absolute path); the
bounds live on the work performed:

- Traversal tools refuse virtual or self-referential roots (`/`, `/proc`,
  `/sys`, `/dev`, `/run`).
- Heavy traversal (glob, content search) runs in a killable subprocess
  under a wall-clock deadline and output cap
  (`file_tools._run_bounded_search` is the shared engine).
- Whole-file reads for agent tools are stat-first, regular-files-only, and
  size-capped (`file_service.check_readable`); oversized reads return an
  actionable error suggesting targeted reads, not a partial or hanging
  read.

## Filesystem Permission Model

Every non-exempt tool call passes through the shared permission executor.
Categories: `file_external` (writes outside the sandbox), `file_internal`
(writes inside), `bash` (non-read-only shell), `notebook` (cell
execution). Each category's auto-approve flag lives in `project_config`
and is read live per call; read-only tools and DB-only tools are exempt
(`permission_executor.py` is the authoritative list).

## Uploads And Downloads

- Validate filenames and content-type expectations; never trust
  client-provided paths; treat uploads as untrusted even single-user.
- Downloads resolve paths through the same path safety layer used for
  reads.
- Archive extraction rejects entries that escape the destination.
- Document and gracefully reject unsupported, encrypted, corrupt,
  oversized, or malformed documents.

## User Content Rendering

HTML generated from Markdown, diffs, documents, model output, or user
files is sanitized before `dangerouslySetInnerHTML`; fallback paths also
sanitize. Prefer structured rendering over raw HTML when practical.

## LLM And External Provider Calls

- Non-streaming LLM calls go through `llm_service`; structured responses
  are parsed and validated before use.
- OpenAI-compatible providers differ in streaming format, tool-call
  shape, reasoning fields, error schema, and timeouts; handle variations
  defensively at the provider boundary.
- Non-LLM external calls with retries, auth, rate limits, or response
  parsing live behind a small client/service wrapper.
- Never log secrets, API keys, sensitive prompts, or raw provider
  responses unless explicitly needed and redacted.

## Shell And Browser Tools

- Shell tools run through the permission and safety layers; read-only
  command classification stays conservative.
- Browser tools avoid exposing raw privileged browser state without a
  clear need.
- Tool inputs are untrusted even when produced by an LLM.
- The backend may run as root: every signal-sending path (`os.kill`,
  `os.killpg`, `proc.kill`) validates its target pid first
  (`bash._kill_process_group` is the reference guard).

## Configuration And Secrets

Configuration and all environment-variable access go through
`core/config.py` — never `os.environ`/`os.getenv()` elsewhere. Secrets
live in `settings.yaml`, never hardcoded. Logs never contain secrets.

## Access Password And Session Cookies

- `settings.yaml` stores only the bcrypt hash under
  `security.password_hash`; plaintext is never persisted or logged. The
  hash is written only by `/auth/password` and the offline
  `backend/scripts/reset_password.py`; settings endpoints always discard
  client-supplied hashes.
- Session cookies are HMAC tokens keyed by a 0600 secret file
  (`userdata/.SiGMA/auth_secret.key`) that rotates on every password
  change, invalidating all outstanding cookies. Cookies are `HttpOnly` +
  `SameSite=Lax`, `Secure` over HTTPS.
- Enforcement is a reject-by-default pure-ASGI middleware covering HTTP
  and WebSocket (unauthenticated handshakes closed with 4401). The public
  allow-list (`AUTH_PUBLIC_PATHS`) stays narrow — every entry widens the
  unauthenticated surface.

## Security Review Checklist

Can user input alter a path, URL, command, selector, prompt, or rendered
HTML? Is validation centralized and reject-by-default? Are symlinks,
traversal, separators, empty names, and hidden names handled? Are errors
informative without leaking secrets?
