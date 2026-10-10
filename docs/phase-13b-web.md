# Phase 13B web foundation

Phase 13B introduced a read-only browser/API foundation alongside the existing
Textual/GTK application. Phase 13D has since added authenticated browser chat
and project activity streaming; those later capabilities are documented in
[Phase 13D browser chat](phase-13d-web-chat.md). Neither phase replaces the TUI,
dispatches tools, executes commands, edits project files, or starts a task runner. The
approved revised Phase 13A plan was not present in the checked-out repository
when this work was implemented; the detailed Phase 13B requirements are the
implementation scope. The web foundation is not authorization to skip any
existing `Agent`, `Tools`, approval, Phase 10 policy, execution-backend, or
Phase 12 memory contract.

## Service configuration and deployment

Install the project dependencies and start the service with `synai-web`.
Configuration is read from the process environment:

| Variable | Purpose |
| --- | --- |
| `SYNAI_INITIAL_PASSWORD` | Initial single-user password, required only before a credential has been provisioned. It is not a default and is never returned by an API. |
| `SYNAI_WORKSPACES` | Operator allowlist, using `key=/absolute/path` entries separated by the platform path separator. Browser input selects only a configured key. |
| `SYNAI_PUBLIC_ORIGIN` | Exact browser origin. Defaults to `http://127.0.0.1:8765`; an HTTPS value enables `Secure` cookies. |
| `SYNAI_BIND_HOST` | Listener address. Defaults to `127.0.0.1`; non-loopback binds require an HTTPS public origin. |
| `SYNAI_PORT` | Listener port; defaults to `8765`. |
| `OLLAMA_URL` / `OLLAMA_HOST` | Existing Ollama provider endpoint. Provider unavailability does not prevent service startup. |

For frontend development, run the API service separately and use `cd web &&
npm run dev`; Vite forwards `/api` requests to `http://127.0.0.1:8765` unless
`SYNAI_API_TARGET` is set. Run `npm test`, `npm run typecheck`, and `npm run
build` from `web/`. Browser acceptance tests use a mocked API and WebSocket
(`npm run test:e2e`) and never contact the configured Ollama service.

Do not place credentials in frontend assets, source control, command-line
arguments, or ordinary logs. Supply the initial password through a protected
secret/environment mechanism for first startup and remove that provisioning
value afterward. Once initialized, startup does not reset or replace the
credential. Change it through the authenticated, CSRF-protected password
endpoint; the change revokes existing sessions, requiring a new login.

The service defaults to loopback. For remote browser access, put an
authenticated HTTPS reverse proxy in front of the service, bind the upstream
to loopback or a private socket/network, firewall the application listener,
and set `SYNAI_PUBLIC_ORIGIN` to the exact external HTTPS origin. Do not expose
the application directly on an untrusted network over HTTP. The service does
not enable CORS or trust arbitrary forwarded-origin headers.

The FastAPI application is created with `synai.web.app.create_app`, injected
configuration/provider/services, and an async lifespan. The OpenAPI document
is served at `/api/openapi.json`; generated TypeScript contracts are checked
into `web/src/api/schema.d.ts` and refreshed with `cd web && npm run api:types`.
All public application routes are under `/api/v1`:

* `GET /api/v1/health` is liveness only and returns `{"status":"alive"}`.
* `GET /api/v1/ready` is an internal readiness indication, not a public
  dependency inventory.
* Authentication routes provide login, session status, CSRF-token refresh,
  logout, and password rotation.
* Authenticated `GET /api/v1/models` asks the existing `ModelProvider` for
  discovery results. A provider failure returns a bounded error; it does not
  disclose internal endpoint details or take the API offline.
* Authenticated project routes register, list, and inspect projects selected
  by configured workspace key and opaque project ID. There is no project
  browsing or mutation route.

Request bodies are bounded (default 1 MiB, configurable only within a fixed
safe range), concurrent requests are bounded (default 64), and body lengths
are enforced both from `Content-Length` and streamed bytes. Errors use a
structured `{error: {code, message, request_id}}` shape and omit exception
text and secrets.

## Authentication and web metadata

The service is single-user. Passwords are Argon2id hashes; session and CSRF
secrets are cryptographically random and only their hashes are persisted.
Sessions are server-side, expire, and can be revoked. The browser session is
an `HttpOnly`, `SameSite=Strict` cookie scoped to `/api/v1`, marked `Secure`
when the configured public origin is HTTPS. State-changing authentication
operations require the exact configured `Origin` and CSRF header. Login
attempts are bounded by a persisted rate limiter keyed by a one-way peer
identifier. No wildcard CORS is configured.

Browser sessions, workspace registrations, login limits, and lease generations
are stored in the private versioned SQLite database at
`~/.synai/web/metadata.sqlite3`, separate from conversation history and the
Phase 12 memory database. There is no history migration. Unknown schema
versions, unsafe metadata permissions, and database errors fail explicitly.

## Workspace registration and path identity

Only operator-configured workspace mounts can be registered. Each root must
be an existing canonical directory, not a symlink, filesystem root, home,
history/data root, or overlapping prohibited location. The registry binds a
key to the canonical path and its current owner/device/inode identity. It
revalidates the path at registration and inspection; replacement, removal, or
identity drift makes a registration stale instead of silently authorizing a
new directory. Similar display names do not share project IDs.

The API returns server-generated opaque project IDs and bounded display/status
metadata. It does not return host paths or mount credentials. Internal types
distinguish host workspace paths, web-container paths, and future runner
mounts; no conversion between them is inferred from a browser-supplied path.
The web API is read-only and registration does not grant mutation authority.
There are no file-read, file-write, terminal, tool-dispatch, task, or memory
editing endpoints.

## Shared data-root ownership

Both the legacy TUI entrypoint and web-service lifespan acquire an exclusive
OS `flock` on `~/.synai/application.lock` before using the shared
application-data root. The lock is the authority; PID/mode/start-time metadata
is diagnostic only and can be stale or malformed without granting ownership.
The lock file is private, opened without following symlinks, and held by file
descriptor. Process termination releases the kernel lock automatically.
Starting the other application while one owns the root fails with an explicit
conflict. This prevents simultaneous web/TUI writes without changing legacy
conversation formats.

## Workspace coordinator contract

`WorkspaceCoordinator` is a foundation for future workspace writers; it is
not wired to an API mutation route. Lock keys derive from validated workspace
identity, not a project ID supplied by a client. The coordinator combines an
in-process async lock with a cross-process exclusive file lock and stores
monotonically increasing lease generations in web metadata. Owner metadata
includes owner, workflow, task, and bounded label identifiers. Reentrancy is
allowed only for the same owner/workflow/task tuple. Waits have an explicit
maximum timeout; cancellation releases a newly acquired OS lock and the
in-process lock. Normal release invalidates the generation and releases both
locks.

The raw fencing secret is returned only to the trusted holder; only its hash
is persisted. `validate_authority(identity, owner, token)` checks the current
persisted generation, status, owner tuple, and token hash. A writer must ask
the authoritative coordinator to validate this token immediately before its
write; possession of an earlier token or an unexpired lease is not sufficient.
The current web application does not itself write workspace files.

If an owner process crashes, the OS lock becomes available but the last
persisted lease remains active. A new writer cannot infer that the old runner
stopped. Recovery requires an explicit trusted recovery authority to confirm
the exact previous identity, owner tuple, generation, and that all prior
workspace-writing processes have been reaped. Only then can a new fencing
generation be issued. A timeout, missing authority, mismatched evidence, or
uncertain state fails closed. A future runner must independently enforce
current generation authority; service-side checks alone are not fencing.

`capture_preimage` uses the existing bounded no-follow repository reader and
records workspace identity, relative path, file device/inode/size, and a
content digest. `preimage_is_current` can detect changes before dispatch or
after an operation, but it cannot make a command atomic. The future runner
must compare the expected preimage at the actual supported file-write
mutation point. Arbitrary terminal commands cannot be claimed to have atomic
conflict protection.

## Runner protocol proposal (not implemented)

The production runner and its transport are intentionally absent in Phase
13B. The following v1 contract is a trust-boundary proposal, not a claim that
a runner is available:

1. **Separate trust domains.** The web process can call only an authenticated,
   narrow runner API; it never receives an unrestricted Docker/Podman socket
   or runtime-administration endpoint. A separately restricted runner owns
   the runtime control channel, fixed image allowlist, mount templates,
   resource policy, and per-project mapping. Only that approved runner may
   mount a selected workspace writable. The browser cannot select images,
   mounts, host paths, privileges, devices, networks, or runtime options.
2. **Service identity and replay prevention.** Use mutually authenticated TLS
   with a runner-pinned web-service client identity. Every request has a
   cryptographically random one-shot request ID, a bounded issued/deadline
   interval, and a canonical payload digest bound to the authenticated
   request. The runner durably claims the request ID before side effects and
   rejects duplicates, expired requests, unknown protocol versions, and
   reused IDs. Transport retries use a new request ID only after a prior
   request is conclusively known not to have started; ambiguous requests are
   queried by ID and never blindly replayed.
3. **Exact workspace and lease binding.** The request names the opaque
   registered project ID and an expected workspace-identity digest; it never
   contains a browser-provided absolute path or arbitrary mount. The runner
   resolves the ID through its own configured project mapping and verifies
   the expected identity and read/write mount policy. Every operation carries
   the current workspace lease generation and opaque fencing token. The
   runner validates current authority against the authoritative lease source
   immediately before starting and at each supported mutation boundary.
4. **Action and authorization provenance.** A strict, versioned request
   schema binds task ID, step ID, action ID, validated tool/operation name,
   schema version, policy version/decision, and any approval record to the
   canonical arguments digest. Missing, ambiguous, stale, or mismatched
   provenance is rejected. Web request data cannot create or substitute an
   approval, policy decision, task identity, or tool identity.
5. **Paths and mutation preconditions.** File-operation paths are normalized
   workspace-relative paths with traversal, absolute paths, alternate
   separators, symlinks, and disallowed file types rejected runner-side.
   Write/patch/delete operations include an expected preimage (identity,
   device/inode where meaningful, size, and digest) and the runner enforces
   it at mutation time using descriptor-relative no-follow access and an
   atomic compare-and-replace strategy where supported. Conflicts return a
   typed conflict result, never an implicit force write. An arbitrary command
   operation does not receive this atomicity guarantee.
6. **Deadlines, cancellation, and resource limits.** Requests carry an
   absolute deadline and a separate unguessable cancellation handle. The
   runner enforces bounded wall time, CPU, memory, process count, writable
   bytes/files, and stdout/stderr bytes from runner-side policy, not from
   client-selected values. Cancellation is authenticated and idempotent; it
   terminates the entire process/container group and confirms writer shutdown
   before releasing the lease.
7. **Results and post-operation evidence.** A versioned response contains
   request ID, typed status/error code, bounded output, validated action
   identity, cancellation/deadline state, and workspace-relative change
   evidence. The runner validates post-operation evidence against current
   no-follow reads while the lease remains authoritative. The web service
   validates response schema, request/project/lease correlation, output
   bounds, and evidence provenance before reporting success. A process exit
   alone is not proof that intended changes or verification results are
   valid.
8. **Crash behavior.** On runner/service crash, no request is automatically
   rerun and no active lease is treated as authorized by age alone. The
   runner records an indeterminate terminal state where possible, reaps and
   confirms all possible writers, then provides exact recovery evidence for
   explicit generation transfer. If it cannot prove that the prior writer is
   stopped, the workspace stays unavailable for writes. Stale generations
   fail closed at the runner.

Before implementation, the runner must still choose and verify its transport
deployment, durable request-ledger retention/backup policy, authoritative
lease-validation channel, OS-level project identity mapping, atomic
filesystem primitives per supported operation, cancellation/reaping
mechanism, and measurable resource ceilings. These decisions must preserve
the existing `Tools` dispatcher, Phase 10 autonomy policy, approval handling,
and execution-backend contracts; the runner must not become a second web
tool dispatcher.

## Phase 12 memory identity compatibility

Phase 12 derives `project_memory_id` from effective UID, device ID, inode, and
canonical workspace path. A host path and a container mount path can therefore
produce different namespaces even when they refer to the same bind-mounted
directory. The required real bind-mount test is present as
`tests/test_web_container_memory_identity.py`; it uses only a disposable
workspace and a copied database. Post-migration validation on Ubuntu 26.04.1
ran the harness through Docker using the service UID and a disposable
dependency-only Python image because no intended SynAI service image was
configured. The real bind mount preserved UID, device, and inode, but the host
temporary path and `/workspace` mount path produced different project-memory
IDs. Retrieval correctly returned `memory_not_found` for that namespace
mismatch, and the copied database was unchanged. The probe also exposed and
fixed read-only database access attempting writable storage initialization.

No migrated Phase 12 database was found under the active `~/.synai` data root
or checkout, so the previous computer's stored identity and compatibility
remain unknown. The disposable-image result is not validation of a deployed
SynAI image, nor evidence of memory compatibility. Do not enable memory-backed
container operation until the intended service image and any existing database
identity have been explicitly checked.

For any confirmed identity mismatch, leave the original database and namespace
untouched. Any reassociation requires a separate operator-reviewed proposal:
show old/new path and identity tuples, enumerate records in a copied database,
require an explicit backup and confirmation, write only into a new namespace
in a transaction, preserve original record payload/provenance and an auditable
mapping, and provide a dry-run plus rollback path. No automatic merge,
rewrite, or silent namespace alias is authorized by this phase.

## Frontend scope

`web/` is a React 19, TypeScript, Vite, Tailwind CSS application using
Radix-based primitives and React Router. API types are generated from the
FastAPI OpenAPI contract. The Slate Dark-only shell supplies login,
Project Command Center, Developer Workbench, not-found, and error states.
The project/workbench screens are structural placeholders; execution and
mutation are explicitly disabled. Layout tokens include focus-visible states,
reduced-motion handling, and responsive collapsed/stacked navigation. Full
chat, Agent Tasks, project memory, model routing, and Workbench operations are
out of scope.

## Compatibility and phase boundary

The existing Python TUI, history and task formats, storage, and Phase 12
memory schema remain authoritative. No historical JSON conversations are
bulk-migrated. Web mode has no chat streaming, WebSocket, task execution,
tool-enabled chat, arbitrary filesystem browser, project mutation, terminal
execution, container administration, automatic checkpoint restore, or
unreviewed policy/routing configuration route. A later phase must revisit the
container memory identity result and complete the independently enforced
runner before enabling any workspace writer.
