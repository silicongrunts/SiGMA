# SiGMA Coding Rules

SiGMA is a single-user, local-first AI research and writing platform with three
modules: Explore, Library, and Synthesis. The backend uses FastAPI and SQLite;
the frontend uses React, Vite, Zustand, CodeMirror, and Tailwind CSS; browser
automation uses Playwright over a shared Chrome CDP connection.

The web app runs as a single process because long-running tasks and SSE stream
coordination use in-process asyncio state. Do not introduce multi-worker or
multi-replica deployment without redesigning that architecture.

## Core Principles

Every change must preserve:

- Clear ownership and module boundaries.
- Simple, readable logic with minimal duplication.
- User work, permissions, and persistent state under invalid input, retries,
  refreshes, cancellation, and concurrent tabs.
- Consistent naming and API contracts.
- Local-first maintainability without multi-tenant or distributed-system
  complexity.

Add abstractions only when they clarify ownership, remove meaningful
duplication, or enforce a shared rule. Users are non-adversarial but
unpredictable; failures must not corrupt data, bypass permissions, silently lose
work, or leave the application without a recovery path.

## Required Rule Files

Read the relevant rules before changing code:

- `RULES/ARCHITECTURE.md`: dependency direction, module ownership, routes,
  services, databases, long-running tasks, and browser automation.
- `RULES/CONTRIBUTING.md`: naming, errors, API conventions, frontend state and
  UI consistency, timers, decomposition, and duplication.
- `RULES/CONCURRENCY.md`: files, databases, ordering, tasks, streams, locks,
  subprocesses, browser state, terminals, and shared mutable state.
- `RULES/SECURITY.md`: paths, uploads, permissions, external calls, rendering,
  secrets, shell/browser tools, and model-generated input.
- `RULES/TESTING.md`: test ownership, verification depth, markers, isolation,
  and required regression coverage.

Before editing, inspect the touched module's owner, callers, public contracts,
tests, and relevant side effects. If a rule conflicts with implementation
reality, align the code or update the rule with an explicit rationale; never
silently ignore it.

## Universal Rules

1. Routes validate and adapt HTTP input/output; business logic, prompts,
   database access, permission decisions, and workflows belong elsewhere.
2. Database access stays inside `backend/app/database/`; routes and tools use
   service APIs rather than repository or ORM internals.
3. Filesystem permission and path-safety decisions use the shared permission
   and path layers. Do not duplicate containment or approval logic.
4. LLM provider calls use the centralized provider boundary. Structured or
   provider-specific responses are validated before becoming internal state.
5. Correctness must not depend on unsafe read-modify-write operations,
   process-local locks, mutable globals, or arbitrary sleeps. Apply
   `RULES/CONCURRENCY.md` whenever persistent or shared state is involved.
6. Do not swallow failures. Broad exceptions are allowed only for documented
   best-effort cleanup that cannot affect correctness.
7. Frontend HTTP, storage, and SSE behavior use their shared helpers. Zustand
   component reads use selectors; product UI uses shared or feature-local UI
   components rather than native dialogs. User-visible text uses
   `react-i18next`, with keys added to every supported locale.
8. Schema changes use Alembic and pass migration-integrity tests. Use batch
   operations for SQLite table rebuilds and preserve dependent FTS structures.
9. Before handoff, review the diff for dead code, dependency violations,
   private API coupling, missing cleanup, edge cases, and tests. Run the
   smallest meaningful verification and report anything skipped.

## Naming Conventions

- Backend services: `foo_service.py`, singleton `foo_service = FooService()`.
- Backend routes: `foo.py`, `router = APIRouter(...)`.
- Repositories: `foo_repo.py`, class `FooRepository`.
- Request and response schemas: domain-specific `FooRequest` and `FooResponse`.
- Exceptions: `FooError(SiGMAException)`.
- Frontend components: `PascalCase.jsx`; hooks: `useThing.js`; utilities:
  descriptive `camelCase` functions in focused files.

Names describe domain meaning, not implementation tricks.

## Updating Rules

Update these files only when a durable architectural boundary, shared
invariant, or objectively reviewable exception changes. Do not record debugging
history, one-off fixes, component internals already documented in code, or rules
that cannot be reviewed or tested. Mention rule changes in the handoff or PR.
