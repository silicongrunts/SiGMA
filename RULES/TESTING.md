# Testing And Verification Rules

Testing scales with risk: no heavy machinery for tiny changes, but
security, concurrency, and cross-layer changes never ship without
meaningful verification. Tests live under `backend/tests/` by product or
domain capability; directory, marker, and entrypoint ownership must stay
obvious. Test code follows the same clarity, dead-code, comment, and
cleanup rules as application code.

## Minimum Expectations

- Pure utility changes: focused unit tests.
- Service changes: service-level tests with mocked externals.
- Route changes: API tests for request/response shape, validation, and
  error translation.
- Agent tool changes: contract tests for success, invalid inputs,
  permission/config failures, and exceptions that must not escape the
  loop.
- Repository/database changes: tests against the migrated SQLite schema,
  not `Base.metadata.create_all()`.
- Worker/stream changes: tests for retry, resume, cancellation, stale
  state, and final failure as applicable.
- Frontend changes: unit tests for hooks/utilities; component or
  browser-level tests when behavior spans components.
- New or changed behavior gets automated tests; doc-only and mechanical
  renames may skip them if the handoff says why.
- Security, concurrency, task-recovery, and data-loss fixes get regression
  tests. A deferral records the bug, recurrence risk, manual verification,
  and the test to add later.

## What To Test

Prefer edge cases over happy paths: empty input; missing resources;
invalid IDs, configuration, and permissions; malformed uploads and LLM
responses; path traversal and symlink cases; duplicate requests;
retry/resume; disconnect/reconnect; concurrent writes to the same owner;
timeouts, cancellations, stale hashes, heartbeats, and task state;
cleanup after success and failure; malformed tool calls and usage
payloads. Prioritize tests that protect user work and recovery paths over
synthetic throughput benchmarks.

## Test Isolation

Write files under `tmp_path` or an isolated fixture; never write real user
data, real `.SiGMA`, home, or repository-root artifacts. Monkeypatch
project, sigma, settings, and user-data paths to temporary roots. Clean up
subprocesses, threads, engines, caches, and read-state fixtures
explicitly; a full test run leaves no business artifacts outside temporary
directories.

The suite runs as root: a mock that can reach a timeout/cancel kill path
gets a real integer `pid` (with `os.killpg` patched) — a bare `MagicMock`
pid coerces to 1 and `os.killpg(1, SIGKILL)` is `kill(-1)`. Never let a
mock reach a signal syscall with an unvalidated target.

## Static Checks

Maintain checks for: route-to-database and tool-to-database boundary
violations; frontend `fetch()` outside API helpers and `localStorage`
outside storage utilities; environment-variable access outside config;
hand-written SSE parsers outside `utils/sse.js`; native
`alert()/confirm()/prompt()` in product UI; repeated UI markup that should
use a shared or feature-local component; bare `except:` and unexplained
`except Exception: pass`; test pollution leaving business artifacts; large
files crossing review thresholds (flag, do not auto-fail). These may be
lint rules, scripts, or CI jobs.

## Verification Notes

When finishing a change, state what was run. If a check cannot run because
dependencies or tooling are missing, say so explicitly instead of implying
full verification. If sandboxing blocks a check, rerun outside the sandbox
when allowed or report the limitation.
