# Concurrency And State Consistency Rules

SiGMA is single-user, but concurrency still exists: multiple tabs, refresh
during a stream, a new task while one runs, uploads during indexing,
reconnect after a worker crash. Treat concurrency as a correctness issue
whenever it can affect persistent data, permissions, task recovery, or
user work. Do not design for multi-tenant scale; prefer simple local-first
mechanisms.

## Risk Model

Acceptable when documented and fixed by refresh/retry/rebuild: transient
UI races, duplicate non-destructive refreshes, auto-rebuilt cache
inconsistency, bounded polling.

Never acceptable:

- Corruption of project files, databases, snapshots, notebooks, or library
  indexes.
- Silent loss of project metadata or user-written content.
- Persisted message/task duplication or reordering that breaks resume.
- Write-permission bypass.
- A long-running task stuck without a visible recovery path.
- Treating an invalid LLM/provider response as valid internal state.

## Core Rules

- Shared mutable state has an owner.
- Persistent-state correctness never depends only on process-local locks
  when tabs, async tasks, or worker threads can touch the same state.
- Read-modify-write uses a transaction, lock, compare-and-swap, unique
  constraint with retry, or another documented mechanism.
- State transitions are idempotent where retry is possible.
- Crashes between steps leave recoverable state.

## Forbidden For Correctness

Never use for correctness-critical persistent logic:

- `max(seq) + 1` without a database constraint and retry strategy.
- Read-modify-write of JSON metadata files without file locking and atomic
  replacement.
- Process-local locks as the only protection for state shared across
  threads, processes, or tabs.
- Mutable module globals as the source of truth for task, browser, stream,
  or project state.
- Arbitrary sleeps to wait for a state transition.

Process-local locks are acceptable for in-memory caches, local
initialization dedup, single-process objects, and coordination whose
documented failure mode is refresh/retry/rebuild. Document whether each
lock is a correctness lock or a coordination/cache lock.

## Database Ordering

- When order matters under concurrent writes, prefer database-enforced
  uniqueness; unique indexes on `(owner_id, seq)` when order is meaningful.
- Prefer transactional counters or append-only records with stable
  ordering; retry on uniqueness conflicts when concurrent writers exist.

## File Writes

- Resolve and validate paths before writing.
- Write to a temporary file and atomically replace for whole-file metadata.
- Use a file lock when multiple processes may write the same file.
- Never expose partial writes as valid state.

## Blocking Work In Agent Tools

Agent tools run on the shared worker event loop; one blocking or spinning
tool call wedges every task in the instance.

- Blocking I/O (file reads, copies, listing/sorting large directories) and
  pure-CPU work (parsing, diffing, base64) run in `asyncio.to_thread` or a
  subprocess — never inline on the loop.
- Unbounded work (recursive walks, content search) runs in a killable
  subprocess under a wall-clock deadline and an output cap; threads cannot
  be killed mid-computation.
- Every tool has a bound — time, output size, or work units. On hitting a
  bound, return partial results with an actionable note or a clean error
  suggesting a narrower path.
- Subprocess pipes are drained concurrently (or closed) before/while
  waiting; a full pipe with a stopped writer deadlocks the awaiter.

## Subprocess Kills And Cancellation

- Spawn shell commands with their own session (`start_new_session=True`)
  so timeout or cancellation kills the whole command tree via
  `os.killpg`; killing only the shell orphans children.
- Never signal an unvalidated pid: `os.killpg(pgid, sig)` is
  `kill(-pgid, sig)`, and a pid of 1 as root is a system-wide SIGKILL.
  Validate `isinstance(pid, int) and pid > 1` first
  (`bash._kill_process_group` is the reference guard).
- Cancelled tool calls clean up their subprocesses in a `CancelledError`
  handler (kill group, bounded reap, close pipes) before re-raising.
- Escalate SIGTERM -> grace -> SIGKILL only when the child handles SIGTERM
  meaningfully; stateless children (rg, ad-hoc commands) may be SIGKILLed
  directly. Always finish with a bounded reap — never an unbounded wait on
  a killed process.

## Worker And Stream State

- Worker tasks are safe to retry or resume where practical; stream state
  tolerates client disconnect/reconnect.
- Heartbeats and task status updates are monotonic or explicitly
  transition-checked.
- Permission and interactive pauses persist through the DB-backed
  `awaiting_input` pause/resume mechanism; workers never block on
  in-memory waits.

## Browser And Terminal State

- Browser tab and terminal session ownership is explicit.
- Reconnect logic distinguishes intentional takeover from transient
  failure.
- Polling only with a bounded interval, cleanup, and an observable
  success/failure condition.

## Review Checklist

Before accepting a change that touches shared state: What owns this state?
Can two requests or workers update it simultaneously? What prevents lost
updates? What happens on a crash halfway through, or on retry? Is the lock
local-only or cross-process?
