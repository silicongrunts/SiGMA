# Contributing And Code Quality Rules

Code should be simple, direct, maintainable, and reviewable from the
modified files plus immediate dependencies.

## General Style

- Prefer clear names over comments.
- Comments are English and explain non-obvious intent, invariants, edge
  cases, or design reasons — never local paths, environment details, chat
  history, or debugging notes.
- Keep functions small enough to understand locally.
- Avoid Boolean flag arguments that create multiple hidden code paths.
- No dead files, exports, functions, parameters, variables, fixtures, or
  tests; document the compatibility reason for any intentionally unused
  item.
- No abstractions for hypothetical future features.

## Naming

- Names describe domain meaning, not implementation tricks; avoid vague
  names like `data`, `result`, `item`, `handler`, `manager`.
- Consistent cross-stack names: `project_id`, `session_id`, `task_id`,
  `annotation_id`, `document_id`.
- File/class naming conventions live in `CLAUDE.md` ("Naming Conventions");
  keep that section the single source of truth.

## Exceptions And Errors

- Custom backend exceptions inherit from `SiGMAException`.
- Business failures raise typed exceptions, not raw error dicts; worker
  result payloads may contain structured error objects.
- No bare `except:`. `except Exception: pass` only for best-effort cleanup
  where the ignored failure cannot affect correctness.
- Logs carry debugging context (project, task, session, document, path
  identifiers); debug logs are never emitted at `error` level.
- User, provider, and configuration mistakes return actionable
  user-facing errors, not crashes, raw logs, or generic fallbacks.

## Backend API Rules

- POST/PUT/PATCH bodies use Pydantic models in
  `backend/app/models/requests.py` or a focused schema module imported
  from there; never `Dict = Body(...)` or untyped dictionaries.
- Use the unified response helper for normal JSON responses; binary
  downloads, streaming, and WebSockets may use framework response types
  directly.

## Frontend API And State Rules

- HTTP through `frontend/src/api/`; storage through `utils/storage.js`;
  SSE parsing through `utils/sse.js`; blob, multipart, and stream requests
  may use `fetch()` inside API helpers.
- Allowed outside `api/`: WebSocket URL construction, native
  `<a download>` links, VNC iframe URLs — extract a helper
  (`getWsUrl()`, `getDownloadHref()`) when one repeats.
- Zustand reads use selectors (`useStore(s => s.field)`); no callback
  refs or event-bus fields in Zustand.
- Props for parent-child communication; React Context for scoped
  cross-tree actions; hooks for reusable stateful behavior.

## Frontend UI Consistency

- No native `alert()`, `confirm()`, or `prompt()` in product workflows;
  use shared modal components or a focused feature-local modal. Replace
  native dialogs in touched workflows when in scope.
- Reuse shared components for repeated modals, confirmations, menus,
  popovers, toasts, controls, upload/edit fields, panels, states, badges,
  rows, and tabs. Search existing components before adding new UI; match
  existing spacing, color, icon, typography, and
  hover/focus/disabled/empty/loading/error conventions.
- Extract the smallest shared component when two features need the same
  pattern; keep feature-specific data shapes feature-local. Split visually
  reusable but behaviorally specific UI into a visual shell plus feature
  logic instead of copying markup.

## Timer Rules

`setTimeout`/`setInterval` are allowed for UI timing, animation, debounce,
retry backoff, and bounded polling — never as a substitute for a missing
readiness signal when a promise, event, or state transition is
implementable. Timers must be cancellable in component cleanup or service
shutdown, use bounded retry/backoff when polling, and never hide race
conditions behind arbitrary sleeps.

## Decomposition And Duplication

Split by ownership, reuse, and correctness — never by line count. Split
when the code has multiple unrelated reasons to change, the same rule or
edge-case handling is needed by more than one feature, or a
security/concurrency/serialization/parsing/protocol rule must stay
behaviorally identical across call sites.

Placement: keep feature-specific details local; move reusable backend
infrastructure to `core/`, domain behavior to services, database-only
behavior to repositories, reusable frontend state to `hooks/`, reusable UI
primitives to `components/`, bulky feature-local UI to a feature folder
such as `components/library/`.

Extract duplicated logic when it encodes a business or security rule,
handles edge cases, must stay behaviorally identical across call sites, or
has changed more than once. Prefer local duplication over a premature
generic abstraction when call sites belong to different domains or the
helper would need many flags, callbacks, or vague names. Name abstractions
after the rule they enforce (`atomic_write_unique_file`,
`allocate_seq_with_retry`, `useClickOutside`, `FileDropzone`), not after
the implementation trick.

Vendored assets (such as noVNC) are exempt from layout rules, but changes
to vendored code must be clearly intentional.
