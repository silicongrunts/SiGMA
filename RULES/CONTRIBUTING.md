# Contributing And Code Quality Rules

Code must be understandable from the modified files and their immediate
dependencies.

## General Style

- Prefer clear names and direct control flow.
- Comments are English and explain intent, invariants, edge cases, or design
  reasons—not debugging history, local environments, or obvious code.
- Avoid Boolean flags that hide unrelated code paths.
- Remove dead files, exports, functions, parameters, variables, fixtures, and
  tests unless a compatibility requirement is documented.
- Do not add abstractions for hypothetical future features.

## Naming

- Names describe domain meaning and stay consistent across layers, especially
  identifiers such as `project_id`, `session_id`, `task_id`, `annotation_id`,
  and `document_id`.
- File and class conventions live in `CLAUDE.md`; keep one source of truth.

## Errors And Logging

- Custom backend exceptions inherit from `SiGMAException`.
- Business failures raise typed exceptions; structured error payloads are used
  only where the contract requires them.
- No bare `except:`. Broad exceptions are allowed only for best-effort cleanup
  that cannot affect correctness and explains the ignored failure.
- Logs include useful domain identifiers without secrets or raw sensitive
  content. Debug information is not logged as an error.
- User, provider, and configuration mistakes produce actionable errors rather
  than crashes, raw logs, or generic fallbacks.

## Backend APIs

- POST, PUT, and PATCH bodies use Pydantic models from
  `backend/app/models/requests.py` or an imported focused schema module.
- Normal JSON uses the unified response helper. Binary downloads, streams, and
  WebSockets may use framework response types directly.

## Frontend APIs And State

- HTTP uses `frontend/src/api/`; storage uses `utils/storage.js`; SSE parsing
  uses `utils/sse.js`.
- WebSocket URLs, native download links, and VNC iframe URLs may be constructed
  outside API helpers; repeated construction belongs in a named helper.
- Zustand component reads use selectors and never act as an event bus.
- Use props for parent-child communication, Context for scoped cross-tree
  actions, and hooks for reusable stateful behavior.

## UI Consistency

- Product workflows do not use native `alert()`, `confirm()`, or `prompt()`.
- Search existing UI before adding components. Reuse established dialogs,
  controls, feedback, status, loading, empty, error, row, tab, and toolbar
  patterns.
- When behavior is feature-specific but presentation repeats, separate the
  reusable visual shell from feature logic.

## Timers

Timers are allowed for animation, debounce, retry backoff, and bounded polling,
not as substitutes for available promises, events, or state transitions. They
must be cancellable during cleanup and must not conceal races with arbitrary
sleeps.

## Decomposition And Duplication

Split by ownership, reuse, and correctness—not line count. Extract logic when a
business, security, concurrency, serialization, parsing, or protocol rule must
remain identical across call sites.

Keep feature-specific details local. Shared backend infrastructure belongs in
`core/`, domain behavior in services, database-only behavior in repositories,
reusable frontend state in hooks, and reusable UI in components.

Prefer local duplication when call sites represent different domains or a
shared helper would require vague names, flags, or callbacks. Name abstractions
after the rule or domain concept they own.

Vendored assets are exempt from project layout rules; modifications to them
must be intentional and explicit.
