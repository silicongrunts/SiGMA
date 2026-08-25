# Architecture Rules

Keep SiGMA layered, understandable, and easy to change. Prefer explicit
ownership over generic abstractions. Core workflow: AI conversation ->
Explore / Library / Synthesis workspace -> project files, knowledge base,
snapshots, notebooks, long-running tasks.

## Backend Dependency Direction

Allowed flow — an allow-list; any cross-boundary dependency not shown here
or documented in this file is forbidden:

```text
routes -> services -> database/repos
routes -> services -> workers
workers -> services
agents/tools -> services
services -> services only through public APIs
```

- Routes do not access repositories, SQLAlchemy models, `UnitOfWork`, or
  worker internals unless explicitly a worker/stream control endpoint.
- Services own business workflows and coordinate repositories, workers,
  LLM calls, filesystem services, and permission services.
- Repositories own SQLAlchemy queries and never leak outside the database
  boundary.
- Tools expose agent capabilities and call services; they never implement
  database or permission logic directly.
- Tool registries, schemas, implementations, and execution context are
  agent-boundary APIs used only by agent orchestration code.
- Never import or mutate private methods or internal state across module
  boundaries without a documented exception.

## Backend Module Responsibilities

- `core/`: config, logging, middleware, lifecycle, response helpers, path
  helpers, shared exceptions.
- `routes/`: HTTP/WebSocket adapters.
- `services/`: domain logic and orchestration.
- `database/`: models, repositories, database manager, migrations
  boundary, unit of work.
- `agents/`: agent registry, prompts, tool declarations.
- `workers/`: Huey tasks, worker-only orchestration, stream relay.
- `models/`: Pydantic request/response schemas.

If a file starts owning two unrelated reasons to change, split it.

## Frontend Dependency Direction

Allowed flow:

```text
views -> hooks -> api/store/utils
views -> components
components -> hooks/api/store/utils
hooks -> api/store/utils
```

- Components must not import views.
- API calls live in `frontend/src/api/index.js` or a focused API module
  exported from there.
- Zustand only for state shared by distant components, never as an event
  bus; reusable stateful logic in hooks; rendering-only reusable UI in
  components.
- Explore, Library, and Synthesis share behavior through hooks, API
  helpers, context, or store state with clear ownership — never hidden
  callbacks or module-specific globals.

## Route Rules

Routes may accept parameters, rely on Pydantic validation, delegate to one
domain service (multiple calls only for validation, response assembly, or
framework adaptation), wrap output in the HTTP response format, and return
framework streaming/file responses. Routes may not build prompts, open
files directly, execute database queries, allocate sequence numbers, make
permission decisions, or contain long workflows.

## Service Rules

Service methods match domain operations: `create_project`,
`compile_project`, `start_ai_reply_stream` — not `do_stuff`, `handle`,
`process_data`. Business failures raise typed exceptions; returning
`{"error": ...}` is acceptable only for worker result payloads or APIs
that explicitly model error objects.

## Database Rules

- Schema changes go through Alembic; existing databases migrate through
  Alembic; new project databases may initialize from models and stamp to
  the current Alembic head.
- Application code does not depend on ORM objects outside `database/`.
- Sequence/order values that must be unique or monotonic under concurrency
  are enforced by the database or a documented transactional mechanism.

## Worker Rules

- Huey task functions are worker entry points, not business services; they
  call services for domain logic.
- Stream relay stays isolated from HTTP route logic except through
  explicit stream APIs.
- Persistent data, task checkpoints, and permission decisions never depend
  solely on worker process memory (auto-approve flags, for example, are
  persisted in `project_config` and read live per call).

## Browser Automation Rules

- Browser automation uses the shared BrowserManager/CDP architecture;
  tools do not launch independent browsers.
- CDP URLs use `127.0.0.1`, not `localhost` (IPv6 ambiguity).
- VNC and readiness polling is allowed when bounded, cancellable, and
  documented in the component or service.

Line count is a review signal, not an architecture rule: at 400+ lines
check responsibility, at 700+ look for real submodules, at 1000+ record a
rationale or decomposition plan. Split by domain responsibility; see
`RULES/CONTRIBUTING.md` for decomposition and extraction rules.
