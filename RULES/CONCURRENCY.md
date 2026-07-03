# Concurrency And State Consistency Rules

SiGMA is locally deployed for a single user, but concurrency still exists:
the user may open multiple browser tabs, refresh during a stream, start another
task while one is running, upload files during indexing, or reconnect after a
worker crash. Treat concurrency as a correctness issue when it can affect
persistent data, permissions, task recovery, or user work.

Do not design for multi-tenant SaaS scale unless the deployment model changes.
Prefer simple local-first mechanisms that make the important state safe.

## Risk Model

Acceptable if documented:

- A transient UI race that a page refresh or retry reliably fixes.
- Duplicate non-destructive refreshes or status fetches.
- Local cache inconsistency that is automatically rebuilt.
- Bounded polling for browser/VNC/readiness state.

Not acceptable:

- Corrupting project files, databases, snapshots, notebooks, or library indexes.
- Losing project metadata or user-written content silently.
- Duplicating or reordering persisted messages/tasks in a way that breaks resume.
- Bypassing file write permissions.
- Leaving a long-running task permanently stuck without a visible recovery path.
- Treating an invalid LLM/provider response as valid internal state.

## Core Rules

- Shared mutable state must have an owner.
- Correctness for persistent state must not depend only on process-local locks
  when multiple web tabs, async tasks, or worker threads can touch the same
  state.
- Read-modify-write operations need a transaction, lock, compare-and-swap,
  unique constraint with retry, or another documented consistency mechanism.
- State transitions should be idempotent where retry is possible.
- Crashes between steps should leave recoverable state.

## Forbidden For Correctness

Do not use these patterns for correctness-critical persistent logic:

- `max(seq) + 1` without a database constraint and retry strategy.
- Direct read-modify-write of JSON metadata files without file locking and
  atomic replacement.
- Process-local `asyncio.Lock` or `threading.Lock` as the only protection for
  state shared across worker threads/processes or browser tabs.
- Mutable module globals as the source of truth for task, browser, stream, or
  project state.
- Arbitrary sleeps to wait for a state transition.

## Acceptable Uses Of Local Locks

Process-local locks are acceptable for:

- In-memory caches.
- Deduplicating local initialization.
- Protecting objects that are guaranteed to be used only inside one process.
- UI or service coordination where a missed lock cannot corrupt persistent
  state.
- Serializing local single-process work when the documented failure mode is
  refresh/retry/rebuild, not data loss.

When using a local lock, document whether it is a correctness lock or only a
local coordination/cache lock.

## Database Ordering

If order matters under concurrent writes:

- Prefer database-enforced uniqueness.
- Prefer transactional counters or append-only records with stable ordering.
- Add unique indexes for `(owner_id, seq)` when `seq` is meaningful.
- Retry on uniqueness conflicts if concurrent writers are expected.

## File Writes

Persistent file writes should:

- Resolve and validate paths before writing.
- Write to a temporary file when replacing whole-file metadata.
- Atomically replace the destination where possible.
- Use a file lock when multiple processes may write the same file.
- Avoid partial writes being visible as valid state.

## Worker And Stream State

- Worker tasks should be safe to retry or resume where practical.
- Stream state should tolerate client disconnect/reconnect.
- Heartbeats and task status updates should be monotonic or explicitly
  transition-checked.
- Permission requests should have a clear timeout, cancellation, and duplicate
  response behavior.

## Browser And Terminal State

- Browser tab ownership and terminal session ownership must be explicit.
- Reconnect logic must distinguish intentional takeover from transient failure.
- Polling is allowed only with a bounded interval, cleanup, and an observable
  success/failure condition.

## Review Checklist

Before accepting a change that touches shared state, answer:

- What owns this state?
- Can two requests/workers update it at the same time?
- What prevents lost updates?
- What happens if the process crashes halfway through?
- What happens if the same operation is retried?
- Is the lock local-only or cross-process?

## Rule Maintenance

When shared-state ownership, locking, retry, ordering, or recovery rules change,
update this file in the same change and mention it in the handoff or PR.
