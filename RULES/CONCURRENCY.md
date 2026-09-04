# Concurrency And State Consistency Rules

Single-user does not mean single-operation. Multiple tabs, async tasks,
uploads, streams, retries, and process restarts can overlap. Use local-first
mechanisms, but treat persistent data, permissions, task recovery, and user work
as correctness concerns.

## Risk Model

Transient UI races, duplicate non-destructive refreshes, bounded polling, and
rebuildable cache inconsistency are acceptable when refresh, retry, or rebuild
restores correctness.

Never accept corrupted files or databases, silent loss of user work, persisted
ordering errors, permission bypass, invalid provider output becoming valid
state, or tasks stuck without a visible recovery path.

## Core Rules

- Shared mutable state has one explicit owner.
- Read-modify-write uses a transaction, lock, compare-and-swap, unique
  constraint with retry, or another documented mechanism.
- Retriable state transitions are idempotent or transition-checked.
- A crash between steps leaves recoverable state.
- Persistent correctness does not depend only on process-local memory.

## Forbidden For Correctness

- `max(seq) + 1` without database enforcement and conflict handling.
- Whole-file metadata updates without locking and atomic replacement.
- Process-local locks as the only protection for state shared across processes
  or persisted across restarts.
- Mutable globals as the durable truth for task, stream, browser, terminal, or
  project state.
- Arbitrary sleeps used as readiness or synchronization mechanisms.

Process-local locks are allowed for single-process coordination, initialization
deduplication, and rebuildable caches. Document whether a lock protects
correctness or only coordination.

## Database And File Writes

- Ordering that matters under concurrent writes uses database uniqueness,
  transactional counters, or stable append-only records with conflict retry.
- Resolve and validate paths before writing.
- Whole-file writes use a temporary file and atomic replacement.
- Use cross-process file locking when multiple processes may write the same
  owner. Never expose partial writes as valid state.

## Event-Loop Work

Agent tools share the web event loop. Blocking I/O and CPU-heavy work run in a
thread or subprocess. Recursive, unbounded, or otherwise non-cancellable work
runs in a killable subprocess with time, output, and work limits.

Subprocess pipes must be drained or closed without deadlocking. On timeout or
cancellation, clean up the whole process tree, close resources, and perform a
bounded reap before returning or re-raising.

Every tool has an explicit bound. When a bound is reached, return a clean error
or useful partial result with guidance for narrowing the operation.

## Task And Stream State

- Database rows are the durable task truth; in-memory cancel, wake, and stream
  state are coordination only.
- Task claiming, leases, status transitions, and checkpoints prevent duplicate
  execution and survive process restart.
- Startup and periodic reconciliation ensure abandoned runnable tasks become
  recoverable or terminal.
- Streams tolerate disconnect and reconnect without treating buffered events as
  durable state.
- Permission and interactive pauses persist before execution stops.

## Browser And Terminal State

- Tab and terminal-session ownership is explicit.
- Reconnect distinguishes intentional takeover from transient failure.
- Polling has a bounded interval, cleanup path, and observable terminal
  condition.

## Review Questions

Who owns the state? Can two operations update it? What prevents lost updates?
What happens after a crash or retry? Is each lock process-local or cross-process?
