# Testing And Verification Rules

Testing should scale with risk. Do not add heavy test machinery for tiny changes,
but do not ship security, concurrency, or cross-layer changes without meaningful
verification.

Backend tests live under `backend/tests/` by product/domain capability. New or
moved tests must make directory, marker, and entrypoint ownership obvious. Test
code follows the same clarity, ownership, dead-code, comment, and cleanup rules
as application code.

## Minimum Expectations

- Pure utility changes: focused unit tests.
- Service changes: service-level tests with mocked external dependencies where
  practical.
- Route changes: API tests for request/response shape, validation, and error
  translation.
- Agent tool changes: contract tests for success, invalid inputs,
  permission/config failures, and exceptions that must not escape the loop.
- Repository/database changes: tests against migrated SQLite schema, not
  `Base.metadata.create_all()`.
- Worker/stream changes: tests for retry, resume, cancellation, stale state, or
  final failure behavior as applicable.
- Frontend hooks/utilities: unit tests.
- Frontend workflows: component or browser-level tests when behavior spans
  components.
- New or changed behavior: add or update automated tests. Doc/comment-only and
  mechanical rename changes may skip tests if the handoff says why.
- Security, concurrency, task-recovery, and data-loss fixes: regression tests,
  unless a deferred test is recorded with rationale.

## What To Test

Prefer edge cases over happy-path-only tests:

- Empty input.
- Missing resources.
- Invalid IDs.
- Invalid user configuration.
- Unsupported or malformed uploaded files.
- Incompatible or malformed LLM API responses.
- Permission denied.
- Path traversal and symlink-like cases.
- Duplicate requests.
- Retry/resume behavior.
- Client disconnect/reconnect.
- Concurrent writes to the same owner.
- Timeout, cancellation, stale hashes, stale heartbeats, and stale task state.
- Cleanup after success and failure.
- Malformed provider responses, tool calls, and usage payloads.

For SiGMA's single-user deployment, prioritize tests that protect user work and
recovery paths over synthetic high-throughput benchmarks.

## Test Isolation

Write files under `tmp_path` or an isolated fixture; never write real user data,
real `.SiGMA`, home, or repository-root artifacts. Monkeypatch project, sigma,
settings, and user-data paths to temporary roots. `NamedTemporaryFile(delete=False)`,
`mkdtemp()`, subprocesses, threads, engines, caches, and read-state fixtures
must be cleaned up explicitly. A full backend test run must leave no project
folders, `.SiGMA` trees, databases, zips, caches, or other business artifacts
outside temporary directories.

## Static Checks

The project should maintain checks for:

- Route-to-database boundary violations.
- Tool-to-database boundary violations.
- Direct frontend `fetch()` outside API helpers, except documented exceptions.
- Direct `localStorage` outside storage utilities.
- Direct environment-variable access (`os.environ`, `os.getenv()`) outside config.
- Hand-written SSE parsers outside `utils/sse.js`.
- Native `alert()`, `confirm()`, or `prompt()` in product UI.
- Repeated UI markup that should use an existing shared or feature-local
  component.
- Bare `except:` and unexplained `except Exception: pass`.
- Test pollution that leaves business artifacts outside temporary directories.
- Large files crossing review thresholds. This check should flag for review,
  not fail automatically on line count alone.

These checks may be implemented with lint rules, small scripts, or CI jobs.

## Verification Notes

When finishing a change, state what was run. If a check cannot be run because
dependencies or tooling are missing, state that clearly instead of implying the
change is fully verified. If sandboxing causes timeouts, permission failures, or
false negatives, rerun outside the sandbox when allowed; otherwise report the
limitation.

## Regression Tests

Every fixed bug must get a regression test unless it is not practical
immediately. If deferred, record:

- The exact bug.
- The risk of recurrence.
- The manual verification performed.
- What automated test should be added later.

## Migration Integrity

`backend/tests/database/test_migration_integrity.py` verifies that `alembic
upgrade head` produces a schema matching `Base.metadata`. If the test is moved,
update this file and mention the move in the handoff or PR. Any model change
without a matching migration must fail this check before merge.

## Rule Maintenance

When test layout, markers, isolation policy, or required coverage changes,
update this file in the same change and mention the update in the handoff or PR.
