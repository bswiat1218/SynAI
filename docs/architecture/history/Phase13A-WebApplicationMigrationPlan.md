# SynAI Phase 13A — Web Application Migration Plan

## Goal and proposed approach

Add a Dockerized browser application without replacing or destabilizing SynAI's
Python engine, the existing Textual/GTK-era TUI, or existing user data. Build a
versioned FastAPI boundary around the existing headless chat and coding-agent
services; introduce browser authentication, explicit project/workspace
registration, persistent web-facing workflow state, and authenticated
real-time event delivery. Deliver the React workbench incrementally, then
complete isolated runner/deployment hardening and a parity-gated rollout.

The initial deployment assumption is the confirmed **single-user,
self-hosted instance with secure browser sessions**. Android clients and
Android-device access are future API consumers only.

## 1. Current architecture and reusable components

### Existing and verified in the repository

- **Normal chat:** `synai/agent.py::Agent.turn` is the independent chat/tool
  loop. It appends messages to a `synai.models.Session`, streams provider
  events into the in-memory `Message`, periodically saves history, dispatches
  native calls through `synai.tools.Tools`, and handles cancellation without
  replaying pending calls.
- **Provider boundary:** `synai/providers/base.py::ModelProvider` is a
  provider protocol; `synai/providers/ollama.py::OllamaProvider` already
  implements model discovery, capability lookup, and streaming chat using the
  configured external Ollama endpoint. No second provider implementation or
  Ollama-in-container requirement is needed for the initial web release.
- **Tool and approval authority:** `Tools.call` validates strict schemas,
  evaluates `AutonomyPolicy`, applies workspace/backend/dispatch guards,
  emits approval/mutation/tool observers, rechecks authority after approval,
  and invokes an `ExecutionBackend`. `CodingApp.approve` is currently a
  Textual-modal callback; it is an adapter point, not an authority that should
  be copied into the web layer.
- **Execution:** `synai/execution_backend.py` defines the backend protocol.
  `synai/sandbox.py::Sandbox` uses the Docker/Podman CLI to create or validate
  a restricted non-root sandbox with one selected workspace mount, read-only
  root, dropped capabilities, no-new-privileges, and resource bounds.
  `synai/host.py::HostExecution` and `synai/sandbox_helper.py` implement the
  separate Linux host mode, explicitly marked not isolated. These safety
  semantics must remain explicit; web deployment must not silently enable
  host mode.
- **Agent Tasks:** `synai/coding_agent/runtime.py::CodingAgentRuntime` is a
  headless runtime with `run_task`, `run_verification`, `run_repair`, and
  `run_review`. It already has typed event callbacks, checkpoint hooks,
  cancellation, policy checks, routing, memory, and deterministic verification
  / read-only review. `synai/coding_agent/state.py` validates plans, task
  states, executions, approvals, and checkpoint payloads. An `AgentCheckpoint`
  currently stores one `AgentTask`, not a project-wide task collection.
- **Context, memory and routing:** `synai/intelligence/RepositoryIndex` and
  `synai/coding_agent/context.py` provide bounded, no-follow repository
  indexing/context selection. `synai/coding_agent/memory.py` provides the
  opt-in private SQLite store and strict evidence/freshness behavior.
  `synai/coding_agent/routing.py` and `Preferences.model_routing` implement
  trusted, deterministic per-stage model selection; `Preferences` also owns
  trusted autonomy and project-memory configuration.
- **Persistence:** `synai/history.py::ManagedHistory` and
  `synai/storage.py::ConversationStorage` store atomic JSON histories in
  private per-conversation folders under `~/.synai`; managed workspaces live
  beside, not inside, their history JSON. History schema versions 1–6 are
  accepted and task recovery marks uncertain operations interrupted rather
  than replaying them. Project memory lives separately at
  `~/.synai/project-memory/memory.sqlite3`.
- **Current UI:** `synai/tui/application.py::CodingApp` owns the provider,
  history, sandbox/host backend, `Tools`, approval modal, cancellation, and
  normal-chat rendering. It is cyberpunk-themed; preferences support themes.
  The TUI does not currently wire `CodingAgentRuntime` into an Agent Tasks
  interface. The runtime's workflows and memory are headless services.
- **Existing checks:** tests use `unittest`, including targeted tests for
  history, tool approvals, sandbox/host execution, coding-agent runtime,
  verification, review, routing, policy, and memory. CI currently tests
  Python 3.12–3.14. There is no FastAPI app, frontend, Docker Compose topology,
  browser-auth system, or web API in the tracked repository.

### Reuse constraints

Keep `Agent`, `Tools`, `ModelProvider`, `ExecutionBackend`,
`CodingAgentRuntime`, `ManagedHistory`, repository intelligence, policy,
verification, review, and memory as the authoritative engine. Add transport
and UI adapters around them; do not fork or reimplement tool dispatch,
verification commands, approvals, task policy, or Ollama requests in
TypeScript. Keep the TUI launch path and behavior available throughout Phases
13B–13F.

## 2. Proposed system/component architecture

```text
Browser (React 19 / TypeScript / Vite)
    ├── Project Command Center
    └── Project Developer Workbench
             │ HTTPS REST + authenticated WSS
             ▼
FastAPI web service (single-user authenticated application boundary)
    ├── auth/session + CSRF/origin checks
    ├── versioned API schemas/routes
    ├── project/workspace registry + task/session/workspace coordination
    ├── event broker + approval broker
    └── adapters to existing SynAI services
             ├── OllamaProvider → configured external Ollama endpoint
             ├── Agent / Tools / CodingAgentRuntime
             ├── ManagedHistory / ProjectMemoryStore
             └── constrained execution-runner interface
                       │ narrow authenticated internal protocol
                       ▼
Dedicated isolated runner boundary
    └── one explicit workspace per execution; no web-container runtime socket

Persistent host bind mounts: existing ~/.synai data and explicitly configured
workspace roots. The `synai-web` service mounts registered project roots
read-only and reads sources through `RepositoryIndex` and the existing
no-follow readers. Only the separately restricted execution runner can mount
the selected root read-write for an authorized operation. Compose serves the
web/API; the browser never receives host paths, runtime credentials, model
credentials, or filesystem authority.
```

Use `synai/web/` for server code and a new `web/` (or `frontend/`, to be
confirmed in Phase 13B) for the Vite application. FastAPI routes should
translate validated requests into engine calls, not become a second engine.
Use dependency-injected service objects, one active turn/task coordinator per
conversation/task, bounded event queues, explicit cancellation, and structured
API errors. Do not keep request-specific approvals or active task objects in
unscoped module globals.

Add a shared, workspace-scoped mutation coordinator keyed by a validated
canonical workspace identity. A durable lock/lease record plus a
cross-process advisory lock prevents overlapping SynAI-controlled mutators.
Agent Tasks hold an exclusive workspace lease while implementation,
verification commands, repair, and restoration can mutate; chat mutations,
checkpoint restoration, and SynAI-managed manual editing use the same
coordinator and cannot overlap that lease. An operation that cannot acquire
the lease fails with a visible conflict/owner status rather than silently
running concurrently. Release leases on normal completion/cancellation and
rely on OS lock release plus explicit interrupted-owner recovery after process
failure. This coordinates SynAI surfaces; non-cooperating external editors
cannot be forced to honor an application lock, so preimage/fingerprint
revalidation must reject conflicts before dispatch/commit and task evidence
must identify intervening changes. This mechanism supplements, never replaces,
existing approval, task-scope, policy, backend, and mutation-attribution
controls.

Expose web state through versioned schemas and a versioned `/api/v1` namespace.
Conversation history remains in the established history format; a separate
versioned task repository is needed for multiple Agent Tasks per project
because the current conversation checkpoint represents one task. Persist
validated task checkpoints before/after consequential boundaries and preserve
the current never-replay-on-recovery semantics.

## 3. Frontend structure, navigation, visual design and responsive layouts

### Information architecture

- **Project Command Center (`/`)**: registered projects, recent sessions,
  model/provider availability, active or interrupted Agent Tasks, latest task
  outcomes, pending approvals, and project-memory availability. Provide clear
  actions to open a project workbench, resume a safe workflow, or create a
  conversation/task. Do not imply that an interrupted operation resumed.
- **Developer Workbench (`/projects/:projectId/workbench`)**: project-scoped
  workspace navigation and one integrated workflow surface for normal chat,
  Agent Tasks, generated/approved plans, execution progress, approvals,
  changed-file diffs, verification, review findings, and project memory.
  Preserve a conventional normal-chat path that does not implicitly start an
  Agent Task.
- **Session/task selection and history**: show saved conversation sessions and
  task states separately; make project, session, and task identity visible.
  Expose model and execution environment without making settings themselves
  grant permissions.

### Visual system and interaction

- React 19, TypeScript, Vite, Tailwind CSS, and shadcn/ui components.
- Slate Dark only: consistent neutral slate surfaces, restrained semantic
  colors for status/approval/errors, readable contrast, clear focus, reduced
  motion, and no persisted theme picker. Replace—not port—the current
  cyberpunk palette.
- Desktop: persistent project/navigation rail, central chat/task timeline,
  contextual right-side plan/diff/activity panel. Tablet: collapsible rail and
  switchable detail panel. Mobile: stacked single-column views, drawer
  navigation, and explicit screens/sheets for approvals and diffs. Resizable
  desktop panels must expose keyboard-adjustable separators, have sensible
  min/max widths, and collapse instead of becoming unusably narrow.
- Keyboard access, semantic landmarks, accessible status announcements, focus
  management for approval dialogs, and usable narrow-screen layouts are
  acceptance criteria, not polish-only work.

### Screen-level wireframes and component responsibilities

**Project Command Center — desktop**

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ SynAI   Projects / Command Center              Ollama: Connected   Account │
├───────────────┬────────────────────────────────────────────────────────────┤
│ PROJECTS      │ Project Command Center                    [New Project]    │
│ ▸ Payments    │ ┌ Project cards / recent workspaces ─────────────────────┐ │
│   Editor      │ │ Payments  · branch/status · Open Workbench              │ │
│               │ └────────────────────────────────────────────────────────┘ │
│ RECENT        │ ┌ Agent Tasks ───────────────┐ ┌ Recent conversations ───┐ │
│ session list  │ │ running / waiting / failed│ │ title · model · updated│ │
│               │ └───────────────────────────┘ └─────────────────────────┘ │
│               │ ┌ Pending approvals ────────┐ ┌ Project memory status ──┐ │
│               │ │ action · scope · Review   │ │ opt-in · records/stale  │ │
└───────────────┴────────────────────────────────────────────────────────────┘
```

**Project Command Center — mobile**

```text
┌──────────────────────────┐
│ ☰  SynAI         Account │  ☰ opens project/history drawer
├──────────────────────────┤
│ Command Center            │
│ [Project picker       ▾] │  Project cards become stacked summaries
│ [Open Workbench]          │
│ Agent Tasks               │  Recent sessions, tasks, approvals and memory
│ [Waiting approval · 1]    │  become ordered single-column cards.
│ Recent conversations      │  Approval opens a full-height review sheet.
│ Project memory            │
└──────────────────────────┘
```

**Developer Workbench — desktop**

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ ‹ Projects   Payments / Workbench     [Chat] [Agent Tasks]  model/status   │
├───────────────┬────────────────────────────────┬───────────────────────────┤
│ PROJECT NAV   │ ACTIVE MODE CONTENT             │ DETAILS (resizable)       │
│ Files (RO)    │ Chat mode:                      │ Task mode:                │
│ Conversations│  chat transcript + composer      │ plan / step dependency    │
│ Agent Tasks   │  independent normal turn         │ execution and approvals   │
│ Project Memory│                                 │ changed files / diff     │
│               │ Agent Task mode:                 │ verification / review    │
│               │  goal + lifecycle timeline       │ memory evidence/status   │
│               │  plan -> approved execution      │ selected item details    │
│               │  -> verification -> review       │                           │
├───────────────┴────────────────────────────────┴───────────────────────────┤
│ Persistent event/status strip: connected · workspace lease · last event   │
└────────────────────────────────────────────────────────────────────────────┘
```

Chat and Agent Task are distinct modes with distinct start actions, lifecycle,
and transcript/task records. Switching modes never converts an ordinary
conversation into a task or makes normal chat inherit task autonomy/approvals.
The workbench may show related chat and task history together but labels their
identities and state separately. Project file browsing uses the web
container's read-only mount. Task-owned edits and resulting diffs are
displayed from validated task-specific evidence, not inferred from tool prose.
The left navigation selects project files (read-only), sessions, Agent Tasks,
and memory. The center owns the active chat or task workflow. The resizable
right panel owns plan, approval context, diff, verification, review, or memory
details; it does not dispatch actions merely because it is open.

**Developer Workbench — mobile**

```text
┌──────────────────────────┐
│ ☰ Payments       [⋯]     │  Drawer: projects, read-only files, sessions,
├──────────────────────────┤  Agent Tasks, memory.
│ [Chat] [Agent Tasks]     │  Persistent mode tabs; mode never changes
│ Task: Add retry policy   │  implicitly.
│ Plan · Step · Status     │  Plan/progress cards stack above conversation.
│ ┌ Current timeline ────┐ │  Details (diff/review/memory) open as a sheet.
│ │ event / chat content │ │  Composer stays reachable above safe-area.
│ └──────────────────────┘ │
│ [Composer          Send] │
└──────────────────────────┘
```

**Approval presentation — all screen sizes**

Show a blocking, focus-trapped dialog/sheet with action/tool name, exact
workspace-relative targets, bounded command or diff preview where applicable,
effect and isolation label, task/step identity, and expiry. Separate plan
approval from per-operation approval. Primary/secondary controls are explicit
`Approve once` and `Deny`; no preselected allow action, blanket grant, or
approval inferred from navigation/keyboard focus. On mobile, use a full-height
sheet with the same preview and decision controls; do not truncate away the
target/effect. On resolution, announce the decision and update the live event
timeline.

**Slate Dark token baseline**

Define tokens centrally as CSS variables/Tailwind theme values (exact shades
verified by contrast tests): `--background: #0f172a` (slate-900),
`--surface: #1e293b` (slate-800), `--surface-raised: #334155` (slate-700),
`--border: #475569` (slate-600), `--text: #f1f5f9` (slate-100),
`--text-muted: #cbd5e1` (slate-300), `--primary: #38bdf8` (sky-400),
`--focus: #7dd3fc` (sky-300), `--success: #34d399` (emerald-400),
`--warning: #fbbf24` (amber-400), and `--danger: #fb7185` (rose-400).
Define separate on-color/text pairs and test actual foreground/background
contrast for normal text, muted text, controls, focus indicators, badges, and
approval/destructive states; replace any token shade that misses WCAG AA.
Use color plus text/icon/status labels, never color alone. Keep spacing,
typography, radii, focus, separators, and reduced-motion tokens consistent
across shadcn/ui primitives.

### Likely frontend organization

`web/src/app/` (router, auth/session bootstrap, API client),
`web/src/components/` (layout, shared Slate/shadcn primitives, approval
dialog, stream/status widgets), `web/src/features/projects/`,
`web/src/features/chat/`, `web/src/features/agent-tasks/`,
`web/src/features/memory/`, and `web/src/styles/`. Keep wire schemas and
runtime validators in `web/src/api/`; use generated API types or a shared
OpenAPI-generated client rather than manually divergent request shapes.

## 4. Backend API endpoints and WebSocket event contracts

### REST contract (proposed)

All routes require the authenticated browser principal except login and
health/readiness. Use JSON request/response schemas, bounded bodies and lists,
opaque IDs, explicit error codes, and authorization checks on every
project/session/task/approval lookup.

| Method and route | Purpose |
| --- | --- |
| `GET /api/v1/health` | Liveness only; no secrets or host path disclosure. |
| `GET /api/v1/ready` | Authenticated dependency/readiness status, including external Ollama reachability as a typed status. |
| `POST /api/v1/auth/login`, `GET /api/v1/auth/session`, `POST /api/v1/auth/logout`, `POST /api/v1/auth/csrf` | Single-user login, current principal/session, logout/revocation, CSRF token. |
| `GET /api/v1/models`, `GET /api/v1/models/{modelId}` | List models and validated capabilities from the configured `ModelProvider`. |
| `GET /api/v1/projects`, `POST /api/v1/projects` | List and register only server-configured, explicit workspace mounts; never accept arbitrary host paths as workspace authority. |
| `GET /api/v1/projects/{projectId}` | Project metadata and bounded status. |
| `GET /api/v1/projects/{projectId}/sessions`, `POST /api/v1/projects/{projectId}/sessions` | List/create conversations backed by `ManagedHistory` and the existing session model. |
| `GET /api/v1/sessions/{sessionId}`, `GET /api/v1/sessions/{sessionId}/messages` | Read validated session state/history with pagination or bounded transcript windows. |
| `POST /api/v1/sessions/{sessionId}/turns`, `POST /api/v1/sessions/{sessionId}/cancel` | Start one normal chat turn or cancel its current work. |
| `GET /api/v1/projects/{projectId}/tasks`, `POST /api/v1/projects/{projectId}/tasks` | List/create separate persisted Agent Tasks; task creation selects mode/model from trusted configuration and existing runtime validation. |
| `GET /api/v1/tasks/{taskId}`, `POST /api/v1/tasks/{taskId}/plan/decision`, `POST /api/v1/tasks/{taskId}/verification`, `POST /api/v1/tasks/{taskId}/repair`, `POST /api/v1/tasks/{taskId}/review`, `POST /api/v1/tasks/{taskId}/cancel` | Inspect and advance only valid task transitions using `CodingAgentRuntime`; no route may skip a lifecycle gate. |
| `GET /api/v1/sessions/{sessionId}/approvals`, `POST /api/v1/sessions/{sessionId}/approvals/{approvalId}/decision` | Enumerate the authenticated user's pending decisions and resolve one bound, unexpired approval exactly once. |
| `GET /api/v1/projects/{projectId}/memory`, `POST /api/v1/projects/{projectId}/memory`, `PATCH /api/v1/projects/{projectId}/memory/{memoryId}`, `POST /api/v1/projects/{projectId}/memory/{memoryId}/archive`, `DELETE /api/v1/projects/{projectId}/memory/{memoryId}` | Surface the existing opt-in memory store's list/add/update/archive/delete behavior and typed failures; explicit user-originated write authorization stays required. |
| `GET /api/v1/sessions/{sessionId}/events` (WebSocket) | Authenticated event stream for chat, tool activity and session state. |
| `GET /api/v1/projects/{projectId}/events` (WebSocket) | Authenticated project/task/approval status stream; subscribe only to authorized resources. |

Avoid a browser route that accepts commands, arbitrary file paths, arbitrary
container options, or unrestricted memory/project IDs. Expose safe workspace
browsing or diff data only through bounded engine readers and validated
workspace-relative paths. OpenAPI is the API contract source.

### WebSocket contract (proposed)

Authenticate the connection using the same server-side browser session cookie
as REST, validate `Origin`, authorize every subscription, cap message/frame
size and connection count, and revalidate authentication/session validity
during long-lived connections. Do not place long-lived bearer credentials in
query strings. Use a single versioned JSON envelope:

```json
{
  "version": 1,
  "event_id": "opaque-monotonic-or-unique-id",
  "type": "chat.delta",
  "timestamp": "RFC-3339 UTC",
  "project_id": "opaque-project-id",
  "session_id": "conversation-id",
  "task_id": null,
  "payload": {}
}
```

Proposed event types include `connection.snapshot`, `session.snapshot`,
`chat.started`, `chat.delta` (bounded `content` and optional
provider-emitted `thinking`), `chat.completed`, `chat.cancelled`,
`tool.started`, `tool.result`, `approval.requested`, `approval.resolved`,
`task.state`, `task.plan_ready`, `task.step`, `task.execution`,
`task.verification`, `task.review`, `task.memory`, `task.completed`,
`task.failed`, `task.interrupted`, `error`, `heartbeat`, and
`resync_required`. Runtime events are observational; a disconnected client or
failed UI subscriber must not reverse checkpointed task state.

Define reconnect/snapshot behavior, event ordering, deduplication, and
backpressure: use bounded queues; drop or coalesce only explicitly
replaceable progress deltas; retain lifecycle and approval transitions; send
`resync_required` plus an authorized REST snapshot rather than silently
claiming a gap-free stream. Never auto-resubmit a turn or mutation after
reconnect.

## 5. Authentication, sessions, approval authorization and security

- Initial product is single-user/self-hosted. Bootstrap one password/secret
  from a secret file or deployment environment; store only a strong
  Argon2id password verifier, support explicit credential rotation, and
  rate-limit login. No default password or unauthenticated first-run admin.
- Issue random opaque server-side browser sessions; store only token hashes,
  expiration/revocation metadata, and timestamps in private persistent data.
  Cookies are `HttpOnly`, `SameSite=Strict` (or justified Lax), `Secure` when
  served over HTTPS, path-scoped, and not readable by JavaScript. Protect
  state-changing REST with CSRF tokens and exact same-origin/allowlisted
  `Origin` checks; disable wildcard CORS.
- Authenticated WebSockets use the cookie-backed session plus same-origin
  validation, not a second unauthenticated port. Logout/revocation closes
  relevant sockets and cancels or explicitly leaves safe server-side work
  according to reviewed lifecycle behavior.
- Bind each approval to principal, session, task/turn, action kind, exact
  tool/argument fingerprint, workspace/backend identity, policy fingerprint,
  issuance time, and expiry. Make each decision one-shot/idempotent. On
  approval, call existing callbacks and let `Tools`/runtime perform their
  post-approval dispatch/policy/preimage checks; browser authorization never
  substitutes for those checks. Plan approval and tool approval remain
  separate gates. Denials execute nothing.
- Recovery invalidates stale UI approval handles; an interrupted/uncertain
  operation is never resumed or replayed. Persist only the necessary typed
  approval lifecycle data; never persist approval as a reusable permission.
- Keep endpoint credentials and bootstrap secrets outside frontend assets,
  history, logs, events, and tool output. Redact session cookies, auth tokens,
  and credentials from errors/logs. Enforce bounded request and response
  sizes, rate limits, secure headers, dependency pinning/review, no
  user-controlled import or shell execution in the web server, and least
  privilege for every container.
- Web mode defaults to isolated runner execution. The UI must visibly
  distinguish unavailable/chat-only, isolated execution, and legacy host
  mode; host mode is not enabled by default and requires the same conspicuous
  explicit authorization as existing SynAI behavior.

## 6. Docker topology, runner isolation, workspace mapping and host mode

### Proposed topology

- `synai-web`: FastAPI plus built static frontend (or a static web service
  behind a same-origin reverse proxy). Runs non-root, read-only root where
  practical, bounded CPU/memory/PIDs, narrow writable tmpfs, dropped
  capabilities, `no-new-privileges`, health checks, and explicit outbound
  access to the configured Ollama endpoint. It receives the required private
  SynAI data mounts and registered project workspace mounts **read-only**.
  It never receives a read-write project mount or runtime socket.
- `synai-runner`: separate isolated execution boundary reached through a
  private authenticated internal protocol. Each tool execution sees only the
  explicitly authorized workspace, bounded working directory, temporary
  filesystem, output/time/resource limits, non-root identity, and controlled
  network. It is the only service that may mount a registered project
  read-write. Web-to-runner requests are typed tool requests, not arbitrary
  runtime administration.
- Existing external Ollama is configured by endpoint/environment. Do not
  assume `localhost` inside the web container refers to the host. Document
  safe host-gateway/remote networking and endpoint allowlisting. Containerized
  Ollama is a future optional profile, not the initial dependency.
- Compose uses named/private persistent storage for new service metadata and
  explicit host bind mounts for existing `~/.synai` and allowlisted workspace
  roots. No implicit mount of `/`, `$HOME`, Docker/Podman socket, SSH
  directories, or arbitrary host paths. Require operator-confirmed mount
  entries and fail closed when paths do not match.

**Docker socket rule:** the web container must never mount an unrestricted
Docker/Podman socket or receive runtime-admin credentials. Define the runner
threat model and wire protocol in Phase 13B, implement and validate the
isolated runner in Phase 13C, and finalize its Compose deployment/hardening in
Phase 13E. The implementation candidate is a dedicated rootless runner/broker
with a constrained runtime identity and narrow authenticated API, or an
isolated remote runner. A socket proxy is not presumed safe merely because it
has filtered endpoints. If the implementation cannot demonstrate an
enforceable workspace/mount allowlist and runtime isolation, tool-based Agent
Tasks remain disabled and chat-only operation remains available.

### Runner protocol and threat model

The API-side adapter remains the policy and orchestration authority:
`Tools.call` validates schema, workspace, task scope, policy, approval, and
preimage; it then delegates only the execution operation. The runner does not
decide whether a request is authorized and does not expose a shell/runtime
administration API to the browser.

- Use a private, authenticated service channel (mutually authenticated local
  transport or equivalent reviewed mechanism) with protocol version,
  one-shot request ID/nonce, short deadline, bounded payload/output, and
  replay rejection. Never accept runner credentials from browser input.
- Each request binds an exact registered project ID and workspace identity,
  canonical host-side identity/fingerprint, task/step/action identity,
  allowlisted tool name, validated workspace-relative arguments, the
  approval/policy/plan-scope fingerprints, cancellation/deadline, and output
  limits. No container image, host path, mount list, runtime option, or
  privilege setting is caller-selectable. The runner independently checks
  request authentication, request replay, project allowlist, workspace
  identity, tool schema, path/no-follow constraints, and resource caps.
- Web-originated file/source inspection stays on read-only mounts using
  existing `RepositoryIndex`/`read_file_bytes` readers. Before a mutation,
  the backend records task-attributed preimage/fingerprint evidence. After the
  runner returns, the backend re-reads the workspace through its read-only
  mount and uses existing `TaskChangeTracker`, `RepositoryIndex`, checkpoint
  and fingerprint checks to establish actual postimages/diffs. The runner may
  return bounded execution result, target list, exit status and digests as
  hints, but those claims are not authoritative source evidence until checked
  against the web backend's read-only view. Terminal commands that affect
  unanticipated paths remain subject to existing task-scope/verification
  limitations and are not overclaimed as confirmed task changes.
- Only the runner receives a read-write mount, and only for one registered
  workspace at a time. It runs non-root with dropped capabilities,
  no-new-privileges, read-only image/root filesystem, private bounded
  temporary storage, CPU/memory/PID/time/output limits, and reviewed network
  access. It cannot read SynAI history, auth/session databases, other project
  roots, SSH material, or host credentials.
- Threats include forged/replayed requests, a compromised web process trying
  to mount another path or exceed tool policy, malicious model/tool output,
  malicious repository contents/commands, runner compromise, workspace
  replacement/symlink races, denial of service, and result tampering. Require
  server authorization plus independent runner request/path/resource checks,
  one-workspace identity binding, no-follow/canonicalization, lease checks,
  output/resource bounds, least privilege, redacted logs, and post-operation
  read-only evidence validation. The runner is still risk reduction, not a
  guarantee against kernel/runtime flaws or approved malicious code.

### Workspace-scoped mutation coordination

Use a single `WorkspaceCoordinator` protocol with in-process async
serialization plus OS-backed cross-process exclusive locks keyed by the
validated canonical project identity (never by client-supplied project ID).
The coordinator issues typed leases for an operation/workflow and records a
bounded owner/reason/expiry for user-visible contention diagnosis.

- A mutating Agent Task holds the exclusive lease across its implementation,
  verification/repair, and any explicitly authorized restoration; it releases
  only after checkpointing its final/interrupted state. Avoid deadlocks by
  defining task-level reentrant ownership and operation-level child checks.
- A normal chat turn can stream concurrently only while it does not mutate.
  Before any chat tool classified as mutating or command-executing, it must
  acquire the same exclusive lease; it cannot dispatch while a task/manual
  editor owns it. Read-only `RepositoryIndex` queries may proceed from the
  read-only mount but must not be presented as fresh across a mutation.
- Repairs and checkpoint restoration use the same lease and retain existing
  plan, policy, approval, all-target preflight, and evidence checks. No second
  mutation is started for a workspace until the current owner releases.
- SynAI-managed manual editor sessions (web or TUI/Neovim integration) acquire
  an interactive mutation lease and keep it until the editor closes; starting
  a task or mutation while held is blocked. Web file edits also go through
  the lease and runner-backed approved mutation backend; the browser/API never
  writes a project mount directly. A TUI editor keeps its existing behavior
  while the TUI owns the exclusive SynAI data-root lock, and cannot race a
  web-owned session.
- External editors/processes that do not cooperate with SynAI cannot be
  prevented by an application lock. Detect their writes with no-follow
  preimage/fingerprint rechecks before runner dispatch and after return; abort
  on mismatch, retain conflict evidence, and require the user to resolve it.
  Never report a stale preimage as unchanged or overwrite an intervening edit.
- Runner refuses execution unless the authenticated request carries the
  current lease identity, and releases/invalidates leases on cancellation,
  timeout, process loss and task recovery. An OS-released lock with stale
  metadata is an interrupted owner, not permission to replay its mutation.

### Workspace and host-mode mapping

- Register projects only from an operator-configured `SYNAI_WORKSPACES`
  allowlist or equivalent explicit Compose mounts. The backend canonicalizes
  and validates paths using existing workspace rules. API input chooses an
  opaque registered project ID, never a new host path.
- Mount registered project roots read-only in `synai-web`; workspace reads,
  Python AST indexing, context assembly, plan validation and read-only review
  source collection use existing bounded no-follow readers. Mount a single
  authorized project read-write only in `synai-runner` after the API has
  authorized the operation and the workspace coordinator lease is valid.
- Keep host-visible canonical workspace identity distinct from
  runner-visible `/workspace`; map the latter only in the isolated runner.
  Record and revalidate canonical path/device/inode/backend identities before
  every operation. Reject symlinks, root/home, history overlap, path escape,
  stale registration, and ambiguous mappings.
- Legacy TUI host mode remains available in the existing TUI path, clearly
  **not isolated**. The web service does not acquire host-mode execution
  authority by virtue of running in Compose. Any later host-mode bridge is
  separately reviewed and user-authorized; default web operation is
  sandboxed or chat-only.

## 7. Data persistence, history migration and project-memory identity

- Treat the current `~/.synai` as user data, not a migration scratch
  directory. Preserve conversation IDs, JSON records, workspace bytes,
  settings, legacy cleanup receipts, project-memory database, and history
  schema compatibility. Reuse `ManagedHistory` for existing JSON histories;
  do not bulk-convert the transcript into new web-only records.
- Add a configurable data-root override only if necessary for Compose, keeping
  `~/.synai` as the default. Before enabling the web service, create a
  permission-checked backup/restore procedure and validate readability of
  histories and memory DB. Unknown/corrupt schemas fail explicitly; never
  reset/overwrite them.
- The web app and TUI share a legacy filesystem format with atomic single-file
  writes but no established cross-process session-write lock. Add an
  application data-root ownership lock respected by both new TUI and web
  startup so both interfaces remain supported but cannot concurrently mutate
  the same store. Make refusal/actionable messaging clear; do not attempt
  last-writer-wins merges.
- Add the workspace-scoped cross-process mutation coordinator as separate
  state from the data-root interface lock. It arbitrates Agent Tasks, chat
  tool mutations, repairs, checkpoint restore, and SynAI-managed editor
  sessions. Protect its lock metadata under private SynAI storage; key locks
  from validated workspace identity, not browser/project IDs. Revalidate
  preimages and fingerprints to detect non-cooperating external edits.
- Use new versioned service metadata tables for opaque project registrations,
  browser auth sessions, pending approval metadata, task indexing/checkpoints,
  and event/reconnect metadata. Keep schemas private, bounded, backed up, and
  separate from conversation JSON and `project-memory/memory.sqlite3`.
  Store task records as validated `AgentTask` payloads; checkpoint mutations
  through the existing session-aware checkpoint hook. Preserve old
  `AgentCheckpoint` v1 and history v1–6 readers; do not reinterpret or
  rewrite a legacy task into a more privileged mode.
- `project_memory_id(workspace)` currently depends on the validated canonical
  path, device/inode, and local UID. Preserve this behavior by using the same
  canonical path and effective UID in the service container when mounts and
  runtime permit; verify the resulting identity rather than assuming
  container-visible paths are equivalent. Stable opaque web project IDs are a
  separate registry identity and must never be passed by a client as a
  ProjectMemoryStore namespace.
- Begin this compatibility proof in Phase 13B with an actual disposable
  container bind-mount test, not a future deployment assumption. Compare host
  and container canonical path, effective UID, device/inode, and computed
  Phase 12 `project_memory_id`; exercise an existing-format disposable memory
  database and verify records remain in the same namespace. Use copied fixture
  data only. A mismatch is a test failure requiring an explicit reviewed plan;
  it must not trigger memory DB writes, automatic namespace creation, merge,
  or reassociation.
- If the web runtime cannot reproduce the existing memory namespace exactly,
  leave old records untouched and offer a separately reviewed, explicit
  migration/reassociation with preview, backup, provenance and collision
  checks. No automatic identity merge, path alias, or silent namespace
  recreation. Moving/replacing a workspace continues to follow the existing
  deliberate identity-change behavior.

## 8. Future Android compatibility (no Android implementation)

- Keep a versioned REST/OpenAPI contract and versioned event envelope
  independent of React routes or browser-only component concepts. Use opaque
  project/session/task IDs, typed lifecycle/state enums, pagination,
  idempotency for start/decision commands, and stable structured errors.
- Reserve a separate principal/client type and authorization policy boundary
  for a future Android **device companion**. It must require explicit pairing,
  user-visible device authorization/revocation, narrow project/action scopes,
  short-lived renewable credentials, auditable approvals, and its own event
  subscription authorization. Do not reuse browser cookies or silently grant
  device access to all workspaces.
- Do not add Android code, device pairing, push notification, or mobile
  permissions in 13B–13F. Verify API contract independence with schema tests
  and document compatibility/versioning policy only.

## 9. Staged migration strategy: Phases 13B–13F

Keep the TUI launch command and tested workflows functional at every phase.
New web paths are opt-in until Phase 13F acceptance. Phase 13B establishes
contracts, runner threat model/protocol, workspace coordination, and an early
real-container project-memory identity test. Phase 13C delivers the Command
Center/chat and implements/validates the isolated runner; chat stays
chat-only whenever no approved runner is available. Phase 13D may enable
tool-based Agent Tasks only after runner and workspace-coordination gates
pass. Phase 13E finalizes Compose and deployment hardening. Phase 13F gates
parity, rollout and rollback.

### Phase 13B — API foundation, identity and compatibility

**Implementation prerequisites**

- The single-user secure browser-session model is confirmed. Before writing
  API/auth code, review the runner trust boundary, workspace lease semantics,
  explicit read-only mount configuration, and the early memory-identity test
  procedure.
- Select the initial supported Docker/Podman test runtime and a disposable
  fixture workspace plus copied Phase 12 memory database. The identity probe
  must run against the intended bind-mount shape and service UID; never point
  it at the live `~/.synai` memory database.
- Treat a failed/ambiguous path, UID, device/inode, or namespace comparison
  as a blocking compatibility result. Do not bypass it by changing a project
  ID, mounting the live database writable, merging records, or recreating a
  memory namespace.

**Milestones**

1. Record architecture/security decisions: deployment path, auth bootstrap,
   API schemas, event envelope, data-root ownership, project registry, runner
   threat model/protocol and mutation-coordination ownership. Specify
   authenticated service identity, one-shot request binding/replay defense,
   per-request workspace allowlist, result validation, failure/cancellation
   behavior, and resource limits. Generate OpenAPI schemas without exposing
   the API publicly.
2. Add FastAPI lifespan/dependency wiring around existing provider/history
   services and a minimal authenticated health/session/model/project read
   surface. Reuse OllamaProvider, PreferencesStore and ManagedHistory.
3. Add secure single-user browser auth, CSRF/origin checks, opaque session
   persistence, explicit read-only project mount registry, and cross-process
   data-root lock. Add `WorkspaceCoordinator` lease semantics/tests for
   competing task, chat mutation, editor, repair and restore operations.
4. Scaffold the React 19/TypeScript/Vite app, screen wireframes, Slate
   Dark/shadcn design tokens and responsive component boundaries; document
   how existing users opt in without changing TUI defaults.
5. Before other work uses project memory, run an opt-in disposable-container
   bind-mount compatibility test with copied data: compare canonical host and
   container paths, effective UID, `st_dev`, `st_ino`, and the exact
   `project_memory_id` formula; open/list/retrieve from a copied Phase 12
   memory database and assert contents and namespace are unchanged. Do not
   mutate, merge, or recreate the user's real memory namespace.

**Likely files/modules**

- New: `synai/web/{__init__,app,dependencies,schemas,auth,errors,projects}.py`,
  `synai/web/routes/`, `synai/web/persistence.py`,
  `synai/web/workspace_locks.py`, runner protocol/threat-model types and
  contract fixtures,
  `web/{package.json,tsconfig.json,vite.config.ts,index.html}`,
  `web/src/{app,api,styles}/`, minimal fixture-only Compose/container
  definitions for identity testing (not production deployment), and focused
  web/lock/identity tests.
- Existing: `pyproject.toml`, `synai/storage.py`, `synai/history.py`,
  `synai/cli.py`, `synai/preferences.py`, `synai/config.py`, `.gitignore`,
  `synai/coding_agent/memory.py`, `synai/coding_agent/changes.py`,
  `.github/workflows/ci.yml`, `README.md`, and architecture documentation.

**New dependencies**

- Python: `fastapi`, `uvicorn[standard]`; use FastAPI/Pydantic request models
  and existing standard-library SQLite unless evidence requires another
  storage package. Password hashing dependency (`argon2-cffi` or an audited
  equivalent) must be selected and pinned/reviewed before auth implementation.
- Web: `react@19`, `react-dom@19`, `typescript`, `vite`, Tailwind CSS,
  shadcn/ui's selected Radix primitives, router, WebSocket-capable API client,
  schema validation, and Vitest/Testing Library. Add only packages actually
  used and pin via the selected lockfile.

**Test coverage**

- FastAPI lifespan, route auth matrix, OpenAPI schemas, strict request bounds,
  login rate limits, cookie flags, CSRF/Origin rejection, session revocation,
  unauthorized project/session lookup, mount allowlist/path/symlink rejection,
  and TUI/web mutually exclusive data-root ownership.
- Workspace coordinator concurrency tests across separate processes and
  in-process async callers: task-vs-chat mutation, editor-vs-task,
  repair-vs-restore, cancellation, timeout, crash/stale owner, and
  preimage-conflict detection. Existing `Tools` policy/approval checks must
  still independently reject unauthorized requests.
- Contract tests for Ollama model listing and existing History v1–6 fixtures;
  verify TUI existing tests still pass.
- Disposable mounted-volume test comparing actual host/container
  `resolve(strict=True)` path, UID, device/inode, and `project_memory_id`;
  prove copied Phase 12 database identity/records are unchanged, and prove a
  deliberately different identity leaves the database untouched with no
  namespace merge/recreation.
- Frontend bootstrap, route guards, API error handling, Slate token/contrast
  baseline, and build/typecheck.

**Completion criteria**

- API can start without a UI and without contacting Ollama for health checks;
  authenticated model listing reports provider failure explicitly.
- API contract and threat model define the runner trust boundary and
  workspace lease before any runner implementation.
- The project-memory identity test passes for the intended supported
  deployment mount, or documents a blocking incompatibility requiring
  explicit design approval; no automatic migration is offered.
- Web workspace mounts are read-only by design; no web route or tool can
  mutate workspace in this phase.
- Existing TUI remains launchable and fully functional when it owns the data
  lock; startup refuses a second writer cleanly.
- Existing conversations and memory DB remain byte-for-byte unchanged in
  fixture tests; no API/Compose fixture exposes a runtime socket or
  read-write project mount to `synai-web`.

**Depends on:** none.

### Phase 13C — Project Command Center, normal chat, live events and runner

**Milestones**

1. Implement explicit project registration/listing and bounded session
   creation/list/read through ManagedHistory. Reuse existing environment and
   model validation; keep project roots read-only in `synai-web`.
2. Add chat turn/cancel service adapters that instantiate/reuse `Agent`,
   `Tools`, and `ModelProvider` with web approval callbacks; preserve normal
   chat as distinct from tasks. Route every mutating chat tool through the
   Phase 13B workspace coordinator and runner adapter.
3. Implement the Phase 13B runner protocol in a separate restricted service
   and validate the threat model: authentic request binding/replay rejection,
   exact project/workspace allowlist, lease verification, read-write access to
   one selected workspace only, bounded resources, cancellation, and
   independently verified results using a disposable test harness. Production
   Compose deployment/hardening remains Phase 13E. `synai-web` remains
   read-only and has no runtime socket.
4. Add authenticated session/project WebSockets, event snapshots/reconnect
   behavior, bounded queues, and a durable-or-resync event strategy.
5. Build the responsive Slate Dark Project Command Center and chat surfaces,
   including empty/error/loading states, model selection, conversation history,
   streaming text/thinking, stop/cancel, and approval dialogs. Provide both
   normal chat and Agent Task entry points but keep Agent Tasks
   tool-disabled until Phase 13D's runner gate is satisfied.

**Likely files/modules**

- New: `synai/web/{sessions,chat,events,approvals,runner_client,
  runner_protocol}.py`, `synai/runner/` or separately built runner service,
  `synai/web/routes/{sessions,models,events}.py`,
  `web/src/features/{projects,chat}/`, `web/src/components/`,
  `tests/test_web_{sessions,chat,events,approvals}.py`,
  runner protocol/threat tests, `web/src/**/*.test.tsx`.
- Existing: `synai/agent.py` (optional typed observer/event hook to emit
  deltas and tool lifecycle without changing authority), `synai/tools.py`
  (reuse/inject callback interfaces only), `synai/execution_backend.py`
  (runner adapter), `synai/history.py`, and docs.

**New dependencies**

- Python: no new conceptual engine; `websockets` support arrives through
  `uvicorn[standard]`. Runner image/runtime dependencies are selected only
  after the Phase 13B threat-model review and stay out of the web image.
  Keep frontend dependencies from 13B; add `lucide-react` or query-state
  library only if chosen.

**Test coverage**

- REST+WS authentication and origin checks; stream sequence, reconnect,
  snapshot/resync, disconnect/backpressure, duplicate turn and cancellation
  behavior; concurrent turn rejection; approval denial/allow-once/expiry,
  post-approval tool guard preservation, no tool replay after restart.
- Runner protocol adversarial tests: forged/missing credentials, nonce replay,
  mismatched project or workspace identity, traversal/symlink target,
  expired/foreign workspace lease, invalid tool schema/approval fingerprint,
  oversized request/result, timeout/cancel/process loss, cross-project mount
  denial, and tampered result claims. Validate preimage/postimage against the
  backend's read-only mount and task-specific change evidence.
- Verify that source browsing/indexing works with `synai-web` read-only and
  that task-specific change evidence remains available from pre-mutation
  snapshots plus validated read-only postimages; do not trust runner prose or
  digests without backend verification.
- UI integration tests for command center, normal chat, streaming, keyboard
  approval interaction, responsive breakpoints and accessible live regions.
- Regression tests for TUI normal chat, stop, provider streaming, approval,
  history load/save, sandbox matching and all previously passing engine tests.

**Completion criteria**

- The browser can list/create/open conversations, discover Ollama models, chat
  and stop a response using existing engine behavior.
- If no runner has passed the Phase 13B threat-model review and Phase 13C
  acceptance, normal chat remains available in explicit chat-only mode and
  every tool-based Agent Task is disabled. Once validated, normal chat tools
  still route through `Tools.call`, existing approvals/policy checks, the
  runner adapter, and workspace coordination.
- Runner validation proves `synai-web` cannot write a workspace, runner
  requests cannot select arbitrary mounts, and only the runner can perform
  authorized workspace mutations.
- The Command Center is usable at desktop/tablet/mobile sizes and reconnect
  loss is explicit rather than silently hidden.

**Depends on:** 13B API/auth contracts, read-only project registry, data-root
lock, runner protocol/threat model, workspace coordinator and early
project-memory compatibility test.

### Phase 13D — Developer Workbench and Agent Task workflow parity

**Entry gate (hard prerequisite):** do not enable tool-based Agent Tasks until
the isolated runner has passed Phase 13C protocol/security tests, the
read-only-web/read-write-runner mount boundary is demonstrated, workspace
coordination concurrency tests pass, task change evidence is independently
validated from the web container's read-only mount, and project-memory
identity compatibility has a passing supported-runtime test (or an explicitly
approved, non-merging migration plan).

**Milestones**

1. Add project-scoped multi-task persistence/indexing and workspace lease
   ownership. Task creation acquires the coordinator before any mutation-capable
   stage; a second task/chat mutation/editor/repair/restore is rejected or
   waits according to the reviewed contention policy, never overlaps.
2. Add task creation/inspection, checkpoint callbacks and recovery semantics
   without changing legacy checkpoint interpretation. Persist task state before
   runner dispatch and after actual read-only postimage validation.
3. Expose runtime transitions for task understanding/context/planning,
   supervised plan approval, scoped implementation, tool approvals,
   cancellation and safe recovery. Gate every runner invocation with the
   task's current workspace lease and existing `Tools`/runtime policy.
4. Expose separate verification, bounded repair, read-only review, diffs/
   checkpoints, task execution evidence, and project memory operations using
   the existing runtime, verifier, repair, reviewer, change tracker and memory
   store. Verification commands share the task lease; repairs and restore
   cannot run concurrently with another workspace mutation.
5. Build Developer Workbench integrated panels for chat, Agent Tasks, plan,
   progress, pending approvals, changed files/diffs, verification, review
   findings and project memory; complete mobile/keyboard states.

**Likely files/modules**

- New: `synai/web/{tasks,task_store,task_events,workspace_readers,memory}.py`,
  `synai/web/workspace_locks.py` (coordinator integration),
  `synai/web/routes/{tasks,memory}.py`, task store schema/migration tests,
  `web/src/features/{agent-tasks,memory,workbench}/`, `web/src/api/`.
- Existing: `synai/coding_agent/runtime.py` (only narrowly typed web-facing
  hooks if required), `synai/coding_agent/state.py` (additive versioned
  serialization only if task persistence demands it),
  `synai/coding_agent/{planner,context,verifier,repair,reviewer,changes,
  checkpoints,memory,routing,policies}.py`,
  `synai/history.py`, `synai/models.py`, and `docs/agent-architecture.md`.

**New dependencies**

- No new engine libraries expected. Existing sqlite3, typed serialization,
  runtime callbacks, `RepositoryIndex`, and `ProjectMemoryStore` remain the
  default. Add frontend diff rendering only after size/sanitization review.

**Test coverage**

- API-to-runtime tests for every supported state transition and rejection of
  out-of-order stage calls; durable checkpoint before/after dispatch; uncertain
  operation recovery without replay; multiple task isolation; bounded plans/
  events/diffs; policy/routing/workspace changes while approvals wait.
- Race tests for Agent Task vs normal-chat write, manual editor vs task,
  verification/repair vs checkpoint restore, multiple task starts, lock
  release on success/cancel/timeout/crash and stale-owner recovery. Confirm
  API lease checks do not replace policy/scope/approval and that the runner
  independently rejects missing/stale leases.
- Verify strict separation of plan approval from tool approval, each terminal
  command's existing approval, review read-only behavior, required verification
  gates, and opt-in memory capture/retrieval and identity scoping.
- Component/e2e tests for entire task lifecycle, reviewer findings, failed
  checks, cancellation, reconnect, pending approval focus, changed-file review,
  memory CRUD and responsive workbench navigation.

**Completion criteria**

- Browser reaches all existing Phase 0–12 user-facing functionality in scope:
  regular chat, Agent Tasks/plans, scoped execution/progress, required
  approvals, verification/repair/review, diffs/checkpoints and opt-in project
  memory, while model routing and autonomy remain trusted configuration.
- Existing engine tests remain authoritative and passing; web controllers do
  not bypass or duplicate security gates.
- Tool-based Agent Tasks are enabled only after the explicit runner/lease/
  evidence/identity entry gate passes; otherwise the workbench reports
  chat-only and does not advertise executable task controls.
- Task history survives service restart and old history/task checkpoints
  remain readable; uncertain operations never replay automatically.

**Depends on:** 13B persistence/auth/coordination/identity baseline, 13C
sessions/events/chat, and Phase 13C runner acceptance. Runner absence or
failed acceptance blocks tool-based tasks; it does not block chat UI work.

### Phase 13E — Docker Compose finalization and deployment hardening

**Milestones**

1. Finalize production Dockerfiles and Compose profiles using the Phase 13C
   runner that passed acceptance; set `synai-web` project mounts read-only and
   runner-only selected-workspace read-write mounts. Ensure web receives no
   runtime socket.
2. Harden non-root web and runner images, Compose network/volume/service
   boundaries, explicit data/workspace bind mounts, UID/GID, external Ollama
   connectivity, resource limits, health checks, logs and secret injection.
3. Re-run the Phase 13B actual identity/namespace test against the production
   Compose profile. Implement explicit backup/restore and only required,
   previewed migration behavior; no automatic memory merge/recreation.
4. Add deployment operations guidance for offline/chat-only mode, external
   Ollama, backups, credentials, permissions, upgrades, safe shutdown, lease
   contention and recovery.

**Likely files/modules**

- New: `Dockerfile`, `compose.yaml`, `.dockerignore`,
  production web/runner build contexts, Compose profiles, container
  integration tests and Compose deployment docs.
- Existing: `synai/sandbox.py` and `synai/execution_backend.py` (backend
  integration only; preserve runtime validation and Phase 13C runner
  protocol), `synai/config.py`,
  `synai/storage.py`, `synai/coding_agent/memory.py`,
  `synai/host.py` only if an explicit compatibility adapter is approved,
  `README.md`, `.github/workflows/ci.yml`, and security tests.

**New dependencies**

- Docker Compose itself is a deployment prerequisite, not a Python package.
  Runner packages/tools remain confined to its own image and only to the
  selected reviewed architecture. Pin base images by supported release/digest
  policy and pin application lockfiles.

**Test coverage**

- Static Compose/config checks: web service has no Docker/Podman socket, no
  privileged mode, no host PID/network, no arbitrary root/home bind, runs
  non-root, mounts project roots read-only, and has resource/health/security
  options. Runner alone receives a read-write mount and only for the
  authenticated selected workspace.
- Explicit mount allowlist and traversal/symlink/history-overlap tests; runner
  request authentication, workspace claim binding, resource/time/output
  limits, process cleanup, cross-project denial, and network policy checks.
- Opt-in disposable-container integration tests only; never manipulate
  unrelated user containers or workspaces. Test with a copied disposable
  `~/.synai` fixture, compare history IDs/content and project-memory namespace
  IDs before/after, verify the Phase 13B host/container UID/path/device/inode
  test remains unchanged, verify backup restore, and prove data is not reset
  on schema mismatch.
- Retest host-mode exclusion and chat-only behavior with runner unavailable.

**Completion criteria**

- A clean deployment starts with explicit mounts and external Ollama, serves
  only authenticated same-origin web traffic, and does not expose an
  unrestricted host runtime socket to the web service.
- The accepted Phase 13C runner is deployed with final Compose hardening;
  `synai-web` project mounts remain read-only, and only the runner can mutate
  an authorized workspace under a valid workspace lease.
- Existing history and memory remain intact; identity equivalence is measured,
  and any necessary reassociation is explicit, reviewed and reversible.

**Depends on:** 13B data/auth/identity contracts, 13C validated runner and
chat/events, and 13D workbench/coordination integration.

### Phase 13F — Parity gate, hardening and controlled rollout

**Milestones**

1. Complete feature-by-feature Phase 0–12 parity inventory; distinguish
   intentionally TUI-only desktop/editor behaviors from required web behavior
   and ensure no required workflow silently disappears.
2. Run end-to-end, accessibility, security, migration, recovery, performance
   and supported-runtime/container acceptance suites; fix only migration-
   related gaps and document remaining non-parity.
3. Ship opt-in web launch/deployment instructions, health/backup/restore/
   rollback runbooks, and retain TUI launch and data ownership paths.
4. Gate any default switch/cutover on explicit acceptance sign-off; preserve
   the previous app path and data backup for rollback.

**Likely files/modules**

- Existing: `README.md`, `docs/agent-architecture.md`, `.github/workflows/ci.yml`,
  `pyproject.toml`, `synai/cli.py`, and all new `synai/web/`, `web/`, Compose,
  runner, migration and test files from 13B–13E.

**New dependencies**

- No new runtime dependency expected. CI may add the Node LTS runtime,
  frontend package manager, frontend test/build steps, and opt-in Playwright
  browser job. Keep container integration opt-in where CI cannot safely
  provide the required isolated runner.

**Test coverage**

- Full existing `python -m unittest discover -s tests -v`, including UI/
  desktop tests where prerequisites exist; frontend typecheck/unit/build;
  browser e2e; Compose validation; OpenAPI compatibility; auth/WS security;
  migration and restart/recovery tests; opt-in isolated execution integration.
- Acceptance drills: TUI-only, web-only, concurrent launch refused safely,
  backup/restore, unavailable Ollama, unavailable runner, interrupted task,
  stale approval, data lock failure, and rollback to TUI with unchanged data.

**Completion criteria**

- All required Phase 0–12 capabilities have explicit parity evidence and no
  security-critical unresolved finding. All published task states and
  approval semantics match runtime behavior.
- Operators can deploy, back up, restore and roll back without deleting or
  converting histories; old and new interfaces are not concurrent writers.
- Web remains opt-in unless the user explicitly approves the cutover; TUI
  remains usable through rollout and rollback.

**Depends on:** 13B–13E.

## 10. Testing and security acceptance criteria

### Functional and regression gates

- Continue running existing `unittest` suites throughout. Add isolated tests
  for adapters and API contracts before browser integration. Frontend
  `typecheck`, unit/component, build and browser tests are separately
  required.
- Maintain tests for normal chat independence, provider stream integrity,
  partial-output retention, stop/cancel semantics, strict schemas, approval
  denial, tool budget, workspace/backend match, capability checks, task
  transitions, routing provenance, required verification, read-only review,
  project-memory freshness and checkpoint recovery.
- In Phase 13B, use disposable, copied data to test the actual supported
  container bind mount: compare host/container canonical workspace path,
  effective UID, `st_dev`, `st_ino`, and Phase 12 namespace hash; exercise
  list/retrieve without modifying the real user's data. Mismatches must leave
  all original namespaces untouched and block assumptions of compatibility.
- Test that web workspace mounts are read-only and all mutation attempts fail
  there, while only the restricted runner can mutate the selected project.
  Verify the web backend can still index/read source and derive changed-file
  evidence by validating preimages and runner results against its read-only
  view.
- Run cross-process workspace lease/race tests for task/chat/manual-editor/
  repair/restore contention, cancellation, process crash and out-of-band file
  modifications. Conflicting fingerprints must fail closed without
  overwriting or claiming unchanged state.
- Add end-to-end acceptance from project registration through chat and a
  supervised Agent Task, plan decision, explicitly approved mutations,
  verification, review, persisted task reload, and a deliberate memory action.
- Use temporary copied test data and isolated workspaces only. Container tests
  remain opt-in and may not touch the user's existing projects or containers.

### Security acceptance gates

- Unauthenticated REST/WS and cross-project/session/task/approval access are
  denied; CSRF, invalid Origin, cookie fixation, expired/revoked sessions,
  brute-force login, oversized payloads and replayed decisions are covered.
- Web app has no Docker/Podman socket, no privileged container permissions,
  no arbitrary host mounts, no root execution, and project mounts are
  read-only. Only the restricted runner can write, and only to one authorized
  workspace with a valid workspace lease. No unrestricted CORS,
  client-controlled shell/path/project namespace, browser-held provider or
  runner secrets, or silent host-mode fallback.
- Tool writes, terminal/network, verification, restore/checkpoint and plan
  actions preserve existing per-operation gates and post-approval validation.
  The runner can only access the selected authorized workspace and bounded
  resources; failure of any isolation, identity or workspace-coordination
  check disables mutation and tool-based Agent Tasks.
- No mutating SynAI-managed workspace operation overlaps another task, chat
  mutation, editor lease, repair or restoration. Non-cooperating external
  edits are detected by preimage/fingerprint checks and cause explicit
  conflict outcomes; they are never overwritten as if uncontended.
- When the runner is absent or fails its review, web remains chat-only and
  cannot invoke a host-mode fallback.
- Review security of auth/session schema, websocket origin/session handling,
  event data exposure, filesystem mapping, SQLite access/permissions,
  Dockerfile/Compose privileges, runner RPC, dependency lockfiles and backup/
  restore before any non-loopback deployment.
- TLS is mandatory outside loopback/trusted local reverse proxy; document
  reverse-proxy headers and trust only explicitly configured proxies.

## 11. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| TUI and web overwrite the same JSON history concurrently | Shared exclusive data-root writer lock in both entrypoints; no two owners at once; preserve atomic history writes. |
| Existing project memory becomes unreachable because container path/UID differs | Test canonical path, UID, device/inode and namespace identity with disposable container mounts in 13B; preserve old records untouched on mismatch and require an explicit, backed-up reassociation. |
| Runner management grants the web service host runtime authority | Mount projects read-only in web and never mount a runtime socket there; threat-model in 13B, implement/validate a separate restricted runner in 13C, and keep tool-based tasks disabled until accepted. |
| Web adapters accidentally bypass policy/approval/runtime state machine | Route all actions through existing `Tools` and `CodingAgentRuntime`; API code can authorize/transport decisions but cannot execute tools itself. |
| Browser disconnect/restart leads to duplicate mutation or lost approval | Idempotent start/decision requests, typed durable checkpoints, expiry/revocation, explicit resync, interrupted state, no automatic replay. |
| Two SynAI workflows or a manual edit race on one workspace | Workspace-scoped cross-process exclusive lease shared by tasks, chat mutations, runner dispatch, restoration and SynAI editors; fingerprint conflicts detect non-cooperating external edits. |
| Web cannot inspect sources or verify runner changes because its mount is read-only | Use existing bounded no-follow `RepositoryIndex`/`read_file_bytes` on web's read-only mount; capture preimages before dispatch and independently validate postimages/diffs after runner completion. |
| Session JSON is too large or current single-task checkpoint blocks task history | Separate bounded versioned task store/index with validated payloads; retain old history/checkpoint decoders and apply size limits. |
| External Ollama endpoint is unavailable or configured unsafely | Keep provider failures typed; explicit trusted endpoint config; startup/chat availability status; no assumption of container `localhost`; no embedded Ollama requirement. |
| Phase 0–12 capabilities do not all have TUI-visible equivalents | Runtime currently headless for Agent Tasks; conduct a capability parity inventory and map each required service to a web surface and acceptance test before rollout. |
| Web exposed beyond local machine with weak auth/TLS assumptions | Single-user secure sessions, no default credential, rate limiting, CSRF/Origin checks, secure cookie, TLS guidance, and explicit reverse-proxy configuration. |
| TUI desktop/editor integrations do not map to browser | Keep these in the working TUI; document as TUI-only unless separately approved for web parity; do not claim parity without evidence. |

## 12. Rollback strategy

1. Keep `synai` and existing TUI startup as the default during implementation;
   web is an explicit opt-in command or Compose profile.
2. Back up the complete `~/.synai` data root before enabling shared web
   history and verify restore into a disposable copy. Do not convert JSON
   history in place.
3. Keep any new web metadata in separate versioned SQLite files/tables with
   reversible additive migrations; never downgrade by deleting unknown
   schema/data. If migration fails, stop web startup and retain both original
   data and a diagnostic backup.
4. On web failure, stop the web/runner services, release the data-root lock,
   verify stored history and memory with existing TUI/tests, then resume TUI.
   If web wrote state, export/retain its metadata; do not discard user task
   history to restore chat usability.
5. If execution isolation fails, disable the runner and leave chat/history
   accessible; do not fall back to host-mode tool execution.
6. Default/cutover switch happens only after Phase 13F sign-off and includes
   tested reverse steps to the TUI release and original data backup.

## 13. Architectural decisions requiring review before implementation

1. **Authentication and deployment boundary:** confirmed single-user
   self-hosted secure browser sessions; finalize credential bootstrap,
   password rotation, supported exposure (loopback-only versus trusted
   reverse proxy), session duration and password-hashing dependency.
2. **Runner isolation architecture:** select a demonstrably constrained
   rootless local runner/broker versus isolated remote runner. Decide exact
   network policy, per-execution versus per-project container lifetime, and
   who can access the narrowly scoped runtime interface. Reject any design
   that gives the web service an unrestricted socket.
3. **Workspace registration and path identity:** define Compose allowlist
   syntax, registration UX, bind-mount ownership/permissions, canonical path
   handling, and behavior when a workspace moves or is replaced.
4. **History ownership and concurrent TUI usage:** confirm one interface at a
   time through the shared data lock, versus an explicitly engineered
   cross-process storage service. Do not permit concurrent uncoordinated JSON
   writers.
5. **Project-memory compatibility:** run the real-container identity probe
    with disposable data in Phase 13B. The existing namespace is never
    automatically merged or recreated; approve an explicit migration/
    reassociation workflow only if the supported mount cannot reproduce the
    legacy path/device/inode/UID identity.
6. **Task persistence shape:** choose additive sidecar SQLite schema versus
    an additive history/checkpoint version for multiple tasks. Preserve legacy
    one-task checkpoints and atomic recovery in either case.
7. **Runner protocol and threat model:** approve authenticated request
    binding/replay protection, service channel, workspace lease binding,
    one-workspace RW mount semantics, cancellation/result validation, network
    policy and failure behavior in 13B; runner implementation must pass 13C
    tests before Agent Tasks may execute tools in 13D.
8. **Workspace coordination and manual edits:** finalize lock lease owner,
    acquisition/reentrancy, timeout/contention UX, cross-process recovery, and
    whether all supported TUI editor integrations can hold the shared lease.
    Non-cooperating external editors are handled as detected fingerprint
    conflicts, never as trusted lock holders.
9. **Event durability and stream semantics:** choose whether lifecycle events
    are persisted in an outbox or reconstructed from task/session snapshots;
    finalize ordering, retention, resync, and backpressure guarantees.
10. **Phase 0–12 parity boundary:** explicitly classify the GTK/editor/Neovim
    desktop behavior and existing theme preferences as TUI-specific versus
    required browser parity. Web visual theme is Slate Dark only; do not
    silently change TUI theme behavior during migration.
11. **Visual and interaction review:** approve the screen-level desktop/mobile
    wireframes, adjustable panel behavior, separate Chat/Agent Task modes,
    approval dialog/sheet content, and contrast-tested Slate Dark token pairs.
12. **External Ollama connectivity:** approve a safe Compose host/remote
    endpoint configuration and outbound network policy; containerized Ollama
    remains optional and out of initial implementation.
13. **Rollout gate:** decide which Phase 13F evidence permits promoting web
    from opt-in to default; keep rollback to TUI available regardless.