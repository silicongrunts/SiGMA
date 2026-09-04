# Security Rules

Security decisions are centralized, testable, and reject by default. User,
file, provider, and model-generated inputs are untrusted even in a single-user
local application.

## Filesystem And Paths

- Resolve paths before decisions and use shared containment helpers, never
  string-prefix checks.
- Account for symlinks when handling user-controlled paths.
- Sanitize uploaded filenames and reject traversal, separators, empty names,
  and hidden names where a plain filename is required.
- Downloads and archive extraction use the same path-safety boundary as reads
  and writes; archive entries may not escape their destination.

## Bounded Agent Filesystem Access

Agent reads may target absolute paths, but traversal refuses virtual or
self-referential roots. Whole-file reads require regular files and size limits.
Recursive search and other potentially unbounded work use killable execution
with time and output limits. Rejections provide a narrower safe alternative.

## Permission Model

Every non-exempt tool call uses the shared permission executor. Permission
categories and exemptions have one authoritative owner. Auto-approval state is
persisted and read at execution time; tools and routes do not duplicate
permission decisions.

## Content And External Calls

- Reject unsupported, encrypted, corrupt, oversized, or malformed uploads and
  documents cleanly.
- Sanitize HTML produced from Markdown, diffs, documents, model output, or user
  files, including fallback rendering paths.
- Non-streaming LLM calls use `llm_service`; provider-specific streaming,
  response, tool-call, reasoning, timeout, and error variations are handled at
  the provider boundary.
- External services with authentication, retries, rate limits, or structured
  responses use a focused client or service wrapper.

## Shell And Browser Tools

- Shell operations use shared permission and safety layers; read-only command
  classification remains conservative.
- Browser tools expose privileged state only when required.
- Validate every PID before sending a signal, especially when running as root.
- Never log secrets, API keys, sensitive prompts, or unredacted provider
  responses.

## Configuration And Secrets

Configuration and all environment-variable access go through
`core/config.py` — never `os.environ`/`os.getenv()` elsewhere. Secrets
live in `settings.yaml`, never hardcoded. Logs never contain secrets.

## Authentication

- Passwords are stored only as secure hashes; plaintext is never persisted or
  logged, and settings updates cannot inject stored password hashes.
- Session tokens are signed with a protected secret. Password changes
  invalidate existing sessions.
- Cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` over HTTPS.
- HTTP and WebSocket authentication rejects by default. Public routes remain a
  narrow explicit allow-list.

## Review Questions

Can input alter a path, URL, command, selector, prompt, or rendered HTML? Is the
decision centralized and reject-by-default? Are traversal, symlinks, malformed
values, and secret-safe errors covered?
