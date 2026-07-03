# SiGMA Coding Rules

This is the entry point for AI agents and contributors. Keep SiGMA readable,
maintainable, robust, and free of accidental architectural drift.

SiGMA is a single-user local AI research and writing platform for students,
independent researchers, and knowledge workers.

The product has three main modules:

- Explore: AI-assisted interactive browsing and information extraction.
- Library: a semantic knowledge base backed by retrieval and reranking.
- Synthesis: Markdown/LaTeX writing, live preview, lightweight snapshot history,
  and Jupyter Notebook support for data analysis.

The UI has a left-side AI conversation and a right-side module workspace. It
connects to user-configured LLM APIs and supports local end-to-end research
workflows.

- Backend: FastAPI, QueryLoop, Huey, SQLite per project, Alembic
- Frontend: React 18, Vite, Zustand, CodeMirror, Tailwind CSS
- Automation: Playwright over a shared Chrome CDP connection

## Core Goal

Every change must move the project toward:

- Clear module boundaries
- Simple, readable logic
- Minimal duplication
- Robust behavior under unexpected user input
- Explicit concurrency and state-transition handling
- Consistent naming and API shape
- Large open-source project maintainability without SaaS-level overdesign

Do not over-engineer. SiGMA is not a multi-tenant SaaS product. Prefer robust,
simple local-first designs over distributed-system complexity. Add abstractions
only when they remove real complexity, prevent meaningful duplication, or make a
boundary clearer.

Users are non-adversarial but unpredictable. They may open two browser tabs,
upload unusual files, enter invalid configuration, connect incompatible LLM
APIs, interrupt long tasks, or refresh the page mid-stream. These cases should
not corrupt data, bypass permissions, lose important work silently, or leave the
app stuck without a recovery path.

## Required Rule Files

Read these files when the change touches the relevant area:

- `RULES/ARCHITECTURE.md`
  - Read before changing backend/frontend layering, services, routes, database
    access, workers, agents, tools, or shared project structure.
- `RULES/CONTRIBUTING.md`
  - Read before changing normal application code. It defines naming, file
    size, exception, API, frontend, and style rules.
- `RULES/CONCURRENCY.md`
  - Read before changing code that writes files, databases, task state, message
    order, locks, workers, streams, browser state, terminal sessions, or any
    shared mutable state.
- `RULES/SECURITY.md`
  - Read before changing filesystem access, uploads/downloads, permissions,
    LLM calls, browser tools, shell tools, external HTTP calls, or user-supplied
    paths/content.
- `RULES/TESTING.md`
  - Read before finishing any non-trivial change or changing backend tests. It
    defines verification levels, test layout, markers, and isolation rules.

If a rule appears to conflict with the implementation reality, do not silently
ignore it. Either align the code with the rule, or update the rule with a clear
exception and rationale.

## Hard Rules

These rules apply to every change.

Before editing code, inspect the touched module's owner, callers, public
contracts, tests, and side effects across routes, schemas, workers, tools,
migrations, or UI state as applicable.

1. Keep routes thin.
   Routes validate input, call services, and format HTTP responses. Business
   logic, prompt construction, database access, and workflow orchestration
   belong outside routes.

2. Keep database access behind the database boundary.
   SQLAlchemy models, sessions, repository internals, and any direct database
   access belong in `backend/app/database/`. Application code uses
   services and `UnitOfWork` APIs; tools and routes should not reach directly
   into repository details.

3. Keep filesystem permission logic centralized.
   Path classification and write approval must go through the shared filesystem
   permission layer. Do not duplicate ad hoc path containment checks.

4. Keep LLM provider calls centralized.
   Non-streaming LLM calls go through `llm_service`. Streaming calls may use the
   dedicated streaming implementation documented in `RULES/ARCHITECTURE.md`.

5. Treat important concurrency as a design requirement.
   Recoverable UI races are acceptable when refresh/retry restores the state.
   Persistent data corruption, permission bypass, duplicate ordering, lost
   project metadata, and unrecoverable task state are not acceptable. Do not use
   `max(seq)+1`, read-modify-write JSON files, process-local locks, or mutable
   globals for correctness unless `RULES/CONCURRENCY.md` explicitly allows the
   case and the limitation is documented.

6. Prefer simple code over clever code.
   If a function cannot be understood locally, split it or name the intermediate
   concepts. If two places implement the same business rule, extract the rule.
   If an abstraction does not clarify ownership or reduce duplication, remove it.
   Do not split code only to reduce line count. Split when it creates a clearer
   owner, a reusable primitive, or a smaller feature-local unit with an obvious
   reason to change. Merge or inline abstractions that have only one caller and
   do not name a stable domain concept.

7. Do not swallow failures silently.
   `except Exception: pass` and bare `except:` are only acceptable for best-effort
   cleanup with a comment or log explaining why the failure is intentionally
   ignored.

8. Keep frontend side effects centralized.
   HTTP calls use `frontend/src/api/`. Local storage uses
   `frontend/src/utils/storage.js`. SSE parsing uses
   `frontend/src/utils/sse.js`. Zustand must use selectors for component reads.
   Repeated UI patterns must use the project's shared components or a
   documented feature-local component for dialogs, menus, toasts, buttons,
   inputs, uploads, editable fields, statuses, loading/empty/error states, rows,
   tabs, and toolbars. Do not add native `alert()`, `confirm()`, or `prompt()`
   for product UI. All user-visible strings must use `react-i18next` (`t()`);
   new keys go in all 8 locale files under `frontend/src/i18n/locales/`.

9. Document intentional exceptions.
   Browser/VNC polling, animation timers, native download links, worker startup
   hooks, and other pragmatic exceptions are allowed only when the relevant rule
   file documents the exception.

10. Verify before handing off.
    Review the diff against the relevant rule files, checking clarity,
    organization, dead code/parameters/files, dependency direction, private API
    use, edge cases, tests, and cleanup. Run the smallest meaningful
    verification; if skipped or unavailable, state that explicitly. Backend test
    changes must keep directory ownership, markers, and isolation rules aligned.

11. Keep schema changes in sync with Alembic migrations.
    Every model change must have a migration generated via `alembic revision
    --autogenerate` and pass
    `backend/tests/database/test_migration_integrity.py`. Never hand-write
    `_alembic_tmp_*` table rebuilds — use `op.batch_alter_table`. After any
    structural change to `library_documents`, recreate the FTS5 triggers.

## Project Shape

Backend source lives under `backend/app/`:

- `core/`: config, logging, lifecycle, response, middleware, shared utilities
- `routes/`: FastAPI route handlers
- `services/`: application/business logic
- `database/`: SQLAlchemy models, migrations boundary, repositories, unit of work
- `agents/`: agent registry, prompts, and tool definitions
- `workers/`: Huey tasks and stream relay
- `models/`: Pydantic request/response schemas

Frontend source lives under `frontend/src/`:

- `views/`: route-level screens
- `components/`: reusable UI components and feature-local UI modules
- `hooks/`: reusable stateful logic
- `api/`: HTTP/SSE/blob API client
- `store/`: Zustand global state
- `utils/`: shared pure utilities and browser wrappers

Do not maintain a full file inventory here. If a directory's purpose changes,
update `RULES/ARCHITECTURE.md`.

## Naming Conventions

- Backend services: `foo_service.py`, singleton `foo_service = FooService()`
- Backend routes: `foo.py`, `router = APIRouter(...)`
- Repositories: `foo_repo.py`, class `FooRepository`
- Request schemas: `FooRequest` or domain-specific existing names in
  `models/requests.py`
- Response schemas: `FooResponse` when a typed response model is needed
- Exceptions: `FooError(SiGMAException)`
- Frontend components: `PascalCase.jsx`
- Frontend hooks: `useThing.js`
- Frontend utilities: descriptive `camelCase` functions in focused files

Names should describe domain meaning, not implementation tricks.

## When To Update These Rules

Update the rule files when:

- A new architectural boundary is introduced.
- A documented exception becomes standard practice.
- A rule is repeatedly impractical and needs a narrower objective test.
- A new class of bug is discovered and can be prevented by a simple rule.

Do not add rules that cannot be objectively reviewed or tested. When a change
requires a rule update, mention the updated rule file in the handoff or PR.
