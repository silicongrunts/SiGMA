# Testing And Verification Rules

Verification scales with risk. Security, concurrency, data-loss, recovery, and
cross-layer changes require meaningful automated coverage. Test code follows
the same clarity, dead-code, comment, and cleanup rules as application code.

## Ownership And Markers

Tests live under `backend/tests/` in the directory that owns the product or
domain behavior. Tool contracts belong with agent tools; route contracts with
routes; repository and migration contracts with database tests; reusable test
data belongs in factories; static dependency checks belong in architecture.

Use registered pytest markers. Directory defaults are defined by
`backend/tests/conftest.py`; explicit markers narrow or override them. Keep
marker registration, collection behavior, and test placement consistent.

## Minimum Expectations

- Pure utilities: focused unit tests.
- Services: service-level behavior with external boundaries controlled.
- Routes: request and response shape, validation, and error translation.
- Agent tools: success, invalid input, permission/configuration failure, and
  exceptions that must not escape the loop.
- Repositories and schema changes: tests against migrated SQLite databases,
  not only `Base.metadata.create_all()`.
- Tasks and streams: retry, resume, cancellation, stale state, disconnect, and
  terminal failure as applicable.
- Frontend behavior: hook or utility tests, plus component or browser coverage
  when behavior crosses components.

Changed behavior gets automated tests. Documentation-only changes and
mechanical renames may skip tests when the handoff explains why. Security,
concurrency, recovery, and data-loss fixes require regression tests; any
deferral records the risk, manual verification, and missing test.

## Test Design

Prioritize edge cases that protect user work: empty or malformed input, missing
resources, invalid configuration or permissions, path traversal and symlinks,
duplicate requests, retry and resume, concurrent writes, disconnect and
reconnect, timeouts, cancellation, stale state, malformed provider/tool
payloads, and cleanup after success or failure.

## Isolation

Write files under `tmp_path` or isolated fixtures. Never write real user data,
real `.SiGMA`, home-directory state, or repository-root artifacts. Redirect
project, settings, and user-data paths to temporary roots. Clean up subprocesses,
threads, engines, caches, and fixtures on both success and failure.

Tests that exercise process termination use valid non-system PIDs and patch the
signal operation. No test may reach a real signal syscall with an unvalidated
target.

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

## Handoff

Run the smallest meaningful checks first, then broader verification when risk
requires it. State exactly what ran. If dependencies, tooling, or sandboxing
prevent a check, report the limitation rather than implying success.
