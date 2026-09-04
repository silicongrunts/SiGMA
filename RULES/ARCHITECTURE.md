# Architecture Rules

Keep SiGMA layered and easy to change. Prefer explicit ownership over generic
abstractions.

## Backend Dependency Direction

Allowed flow — an allow-list; any cross-boundary dependency not shown here
or documented in this file is forbidden:

```text
routes -> services -> database/repos
agents/tools -> services
services -> services only through public APIs
```

- Routes do not access repositories, SQLAlchemy models, or `UnitOfWork`
  directly.
- Services own business workflows and coordinate repositories,
  LLM calls, filesystem services, and permission services.
- Repositories own SQLAlchemy queries and never leak outside the database
  boundary.
- Tools expose agent capabilities and call services; they never implement
  database or permission logic directly.
- Tool registries, schemas, implementations, and execution context are
  agent-boundary APIs used only by agent orchestration code.
- Never import or mutate private methods or internal state across module
  boundaries without a documented exception.

## Backend Ownership

- `core/`: configuration, logging, lifecycle, middleware, shared exceptions,
  response helpers, and reusable infrastructure.
- `routes/`: HTTP and WebSocket adapters.
- `services/`: domain behavior and workflow orchestration.
- `database/`: ORM models, repositories, database management, migrations, and
  units of work.
- `agents/`: prompts, agent registration, tool declarations, and execution
  boundaries.
- `models/`: request and response schemas.

Split a file when it owns unrelated reasons to change, not merely because it is
long.

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

## Routes And Services

Routes validate input, call domain services, translate errors, and format
framework responses. They do not build prompts, open project files, query the
database, allocate ordering values, decide permissions, or own workflows.

Service methods represent domain operations and expose stable public APIs.
Business failures raise typed exceptions; structured error objects are allowed
only where the API contract explicitly models them.

## Database Boundary

- Schema changes go through Alembic. Existing databases migrate; newly created
  databases are stamped to the current head.
- ORM objects remain inside `database/`.
- Unique or monotonic ordering is enforced by the database or a documented
  transactional mechanism.
- SQLite table rebuilds use Alembic batch operations. Structural changes to
  `library_documents` must preserve or recreate its FTS5 triggers.

## Long-Running Tasks

- Chat, annotation, and library work runs as asyncio tasks in one web process.
- Database rows are the durable task truth; in-memory events and stream buffers
  are coordination only.
- Cancellation, retry, disconnect, and process restart must leave tasks
  recoverable or visibly terminal.
- Persistent checkpoints and permission decisions never depend solely on
  process memory.
- Multi-worker or multi-replica deployment is forbidden until task ownership
  and stream coordination are redesigned for it.

## Browser Automation

- Browser tools use the shared BrowserManager/CDP architecture and do not
  launch independent browsers.
- Polling is bounded, cancellable, cleaned up, and tied to an observable
  success or failure condition.
