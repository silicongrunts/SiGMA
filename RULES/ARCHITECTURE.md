# Architecture Rules

SiGMA is a single-user local AI research and writing platform. Its architecture
should remain layered, understandable, and easy to change without turning every
feature into a framework. Prefer explicit ownership over clever generic
abstractions.

Core workflow: AI conversation -> Explore / Library / Synthesis workspace ->
project files, knowledge base, snapshots, notebooks, and long-running tasks.

Design for one local user who may still create overlapping actions by opening
multiple browser tabs, refreshing during streams, interrupting tasks, or changing
configuration while work is in progress.

## Backend Dependency Direction

Allowed high-level flow:

```text
routes -> services -> database/repos
routes -> services -> workers
workers -> services
agents/tools -> services
services -> services only through public APIs
```

Rules:

- Treat the flow above as an allow-list. Any cross-boundary dependency not shown
  here or explicitly documented in this file is forbidden.
- Routes do not access repositories, SQLAlchemy models, `UnitOfWork`, or worker
  internals unless the route is explicitly a worker/stream control endpoint.
- Services own business workflows and may coordinate repositories, workers,
  LLM calls, filesystem services, and permission services.
- Repositories own SQLAlchemy queries and return plain data or ORM objects only
  within the database boundary.
- Tools expose agent capabilities. They should call services instead of
  implementing database or permission logic directly.
- Tool registries, schemas, implementations, and execution context are
  agent-boundary APIs. Only agent orchestration/execution code may use them.
- Do not import or call private methods or mutable internal state across module
  boundaries unless the exception is documented.

Single-user deployment does not remove the need for boundaries. Boundaries keep
the codebase understandable and make unusual user behavior easier to recover
from.

## Backend Module Responsibilities

- `core/`: cross-cutting infrastructure such as config, logging, middleware,
  response helpers, lifecycle hooks, path helpers, and shared exceptions.
- `routes/`: HTTP/WebSocket adapters.
- `services/`: domain logic and orchestration.
- `database/`: SQLAlchemy models, repository classes, database manager,
  migrations boundary, and unit of work.
- `agents/`: agent registry, prompt definitions, and tool declarations.
- `workers/`: Huey task definitions, worker-only orchestration, stream relay.
- `models/`: Pydantic request/response schemas.

If a file starts owning two unrelated reasons to change, split it.

## Decomposition And Reuse

Line count is a review signal, not an architecture rule. Split by ownership,
reuse, and correctness boundaries; merge wrappers that add no boundary value.

Split code when at least one of these is true:

- The code has multiple unrelated reasons to change.
- The same rule or edge-case handling is needed by more than one feature.
- A security, concurrency, serialization, parsing, or protocol rule must stay
  behaviorally identical across call sites.
- A feature-local submodule has a stable domain name and reduces the amount of
  context needed to understand the parent.

Do not split when the only benefit is reducing line count, the helper has one
caller and no stable domain meaning, or the abstraction hides simple local logic
behind flags, callbacks, or vague names.

Prefer these ownership levels:

1. Keep simple, feature-specific details local.
2. Move reusable UI behavior to `frontend/src/hooks/` or reusable UI primitives
   to `frontend/src/components/`.
3. Move feature-specific but bulky UI modules to a feature-local folder such as
   `components/library/` or `components/chat/`.
4. Move cross-cutting backend infrastructure to `backend/app/core/`.
5. Move backend domain behavior to services, and database-only behavior to
   repositories or small repository helpers.

Feature-local modules may be specific. Global utilities must be genuinely
general. For example, a `FileDropzone` component can be shared by file-tree and
library uploads; a `LibraryDocumentRow` should remain in the Library feature.

## Frontend Dependency Direction

Allowed high-level flow:

```text
views -> hooks -> api/store/utils
views -> components
components -> hooks/api/store/utils
hooks -> api/store/utils
```

Rules:

- Components must not import views.
- API calls live in `frontend/src/api/index.js` or a focused API module exported
  from there.
- Shared state lives in Zustand only when multiple distant components need the
  same state. Do not use Zustand as an event bus.
- Reusable stateful logic belongs in hooks.
- Rendering-only reusable UI belongs in components.

The left-side AI conversation and right-side module workspace should share
behavior through hooks, API helpers, context, or store state with clear ownership.
Do not couple Explore, Library, and Synthesis through hidden callbacks or
module-specific global state.

## Route Rules

Routes may:

- Accept path/query/body parameters.
- Rely on Pydantic validation.
- Usually delegate to one domain service. Multiple service calls are allowed
  only for validation, response assembly, or framework adaptation.
- Wrap service output in the HTTP response format.
- Return framework-specific streaming/file responses when needed.

Routes may not:

- Build prompts.
- Open files directly.
- Execute database queries.
- Allocate sequence numbers.
- Implement permission decisions.
- Contain long workflows.

## Service Rules

Services should expose public methods that match domain operations:

- Good: `create_project`, `compile_project`, `start_ai_reply_stream`
- Bad: `do_stuff`, `handle`, `process_data`

Service methods should raise typed exceptions for business failures. Returning
`{"error": ...}` is only acceptable for worker result payloads or APIs that
explicitly model error objects.

## Database Rules

- Schema changes go through Alembic.
- Existing databases migrate through Alembic.
- New project databases may be initialized from models and stamped to the
  current Alembic head if that remains the documented initialization path.
- Application code should not depend on ORM model objects outside
  `database/`.
- Sequence/order values that must be unique or monotonic under concurrency must
  be enforced by the database or by a documented transactional mechanism.

## Worker Rules

- Huey task functions are worker entry points, not general business services.
- Worker tasks should call services for domain logic.
- Stream relay code should stay isolated from HTTP route logic except through
  explicit stream APIs.
- Process-local state in workers may be acceptable for the documented local
  single-user deployment, but the failure mode must be recoverable. Persistent
  project data, task checkpoints, and permission decisions should not depend
  solely on process memory.

## Browser Automation Rules

- Browser automation uses the shared BrowserManager/CDP architecture.
- CDP URLs should use `127.0.0.1` instead of `localhost` to avoid IPv6 ambiguity.
- Browser tools should not launch independent browsers unless a documented
  architecture change is made.
- VNC and browser readiness polling is allowed when bounded, cancellable, and
  documented in the component or service.

## File Size And Complexity

Line count is only a review signal, never the only reason to split. At 400+
lines check responsibility; at 700+ look for real submodules; at 1000+ record a
short rationale or decomposition plan. Split by domain responsibility, not
arbitrary helper buckets.

## Rule Maintenance

When module ownership, dependency direction, or documented architecture
exceptions change, update this file in the same change and mention it in the
handoff or PR.
