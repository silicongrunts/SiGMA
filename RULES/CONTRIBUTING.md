# Contributing And Code Quality Rules

Code should be simple, direct, maintainable, and reviewable from the modified
files plus immediate dependencies.

## General Style

- Prefer clear names over comments.
- Code and test comments must be English and explain non-obvious intent,
  invariants, edge cases, or design reasons. Do not record local paths,
  environment details, chat history, or debugging notes.
- Keep functions small enough to understand locally.
- Avoid Boolean flag arguments that create multiple hidden code paths; split the
  operation when the behavior is meaningfully different.
- Avoid duplicate business logic. Extract the smallest useful shared function.
- Do not leave dead files, exports, functions, parameters, variables, fixtures,
  or tests. Document the compatibility reason for any intentionally unused item.
- Do not add abstractions only because a future feature might need them.
- Do not split code only because a file is long. Split by responsibility,
  reusable behavior, or feature-local ownership.
- Do not create shared utilities for one-off feature details. Keep them local
  until there is real reuse or a security/concurrency rule to centralize.

## Naming

- Names should describe domain meaning.
- Avoid vague names such as `data`, `result`, `item`, `handler`, or `manager`
  when a more specific name is obvious.
- Use consistent names for the same concept across backend and frontend:
  `project_id`, `session_id`, `task_id`, `annotation_id`, `document_id`.
- Keep public method names action-oriented and specific.

## Exceptions And Errors

- Custom backend exceptions inherit from `SiGMAException`.
- Business failures should raise typed exceptions, not return raw error dicts.
- Worker result payloads may contain structured error objects because they are
  persisted task results.
- Do not use bare `except:`.
- Do not use `except Exception: pass` unless it is best-effort cleanup and the
  ignored failure cannot affect correctness.
- Logs should include enough context to debug: project, task, session, document,
  or path identifiers where applicable.
- Debug logs must not be emitted at `error` level.
- User, provider, and configuration mistakes should return actionable
  user-facing errors instead of crashes, raw logs, or generic fallback messages.

## Backend API Rules

- POST/PUT/PATCH request bodies use Pydantic models in
  `backend/app/models/requests.py` or a focused schema module imported from
  there.
- Do not use `Dict = Body(...)` or untyped request dictionaries in routes.
- Use the unified response helper for normal JSON responses.
- Binary downloads, streaming responses, and WebSockets may use framework
  response types directly.

## Frontend API Rules

- HTTP calls to backend endpoints go through `frontend/src/api/`.
- Exceptions allowed outside `api/`:
  - WebSocket URL construction.
  - Native `<a download>` links when browser download behavior is required.
  - VNC iframe URLs.
- If an exception repeats, provide a small helper such as `getWsUrl()` or
  `getDownloadHref()` instead of duplicating string construction.
- SSE parsing is centralized in `frontend/src/utils/sse.js`.
- Blob, multipart, and stream requests may use `fetch()` inside API helpers.

## Frontend State Rules

- Zustand reads in components should use selectors:
  `useStore(s => s.field)`.
- Do not store callback refs or event-bus fields in Zustand.
- Use props for parent-child communication.
- Use React Context for scoped cross-tree actions.
- Use hooks for reusable stateful behavior.
- Browser storage access goes through `frontend/src/utils/storage.js`.

## Frontend UI Consistency

User-facing UI must reuse SiGMA components and visual patterns.

- Do not add `alert()`, `confirm()`, or `prompt()` for product workflows. Use
  shared modal components or add a focused feature-local modal.
- If a touched workflow already uses a native browser dialog, replace it with
  the matching SiGMA modal unless that would expand the task far beyond scope.
- Reuse shared components for repeated modals, confirmations, menus, popovers,
  toasts, controls, upload/edit fields, panels, states, badges, rows, and tabs.
- If a repeated pattern is needed by two features, extract the smallest shared
  component or hook. If it is specific to one feature's data shape, keep it in
  that feature.
- Before adding new UI, search existing components for a matching pattern. New
  UI should match existing spacing, radius, color, icon, typography, hover,
  focus, disabled, empty, loading, and error-state conventions before
  introducing a new visual style.
- If a component is visually reusable but behaviorally feature-specific, split
  the visual shell from the feature logic instead of copying markup.

## Timer Rules

`setTimeout` and `setInterval` are allowed for UI timing, animation, debounce,
retry backoff, and bounded polling. They are not allowed as a substitute for
missing readiness signals when a proper promise/event/state transition can be
implemented.

Timer usage must:

- Be cancellable in component cleanup or service shutdown.
- Have a bounded retry/backoff policy when polling.
- Not hide race conditions by relying on arbitrary sleeps.

## Duplication Rules

Duplication is acceptable only when extracting would make the code harder to
understand. Extract when:

- The duplicated logic encodes a business/security rule.
- The duplicated logic has already changed more than once.
- The duplicated logic handles edge cases.
- Two call sites must stay behaviorally identical.

Prefer local duplication over a premature generic abstraction when:

- The two call sites are superficially similar but belong to different domains.
- The extracted function would need many flags, callbacks, or optional
  parameters to support both places.
- The helper name would be vague, such as `handleItem`, `processData`, or
  `commonUtils`.

When extraction is justified, keep the abstraction small and name it after the
rule it enforces, not after the implementation trick. Good examples:

- `atomic_write_unique_file` for create-only filename conflict handling.
- `allocate_seq_with_retry` for unique sequence allocation.
- `useClickOutside` for popover/menu dismissal behavior.
- `FileDropzone` for reusable file-selection UI.

## Generated Or Vendored Code

Vendored assets such as noVNC are not held to SiGMA source layout rules, but
changes to vendored code must be clearly intentional.

## Rule Maintenance

When style, API, frontend, or vendored-code conventions change, update this file
in the same change and mention it in the handoff or PR.
