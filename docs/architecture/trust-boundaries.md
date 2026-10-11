# SynAI 2.0 Trust Boundaries

This document records the mandatory architecture invariants and the current
resource-authority matrix. Invariants are normative; they do not imply that a
future Broker or client-side patch workflow is already implemented.
Implementation status is summarized in
[the canonical architecture](./synai-2.0.md).

## Mandatory invariants

1. SynAI Core is an orchestration control plane and must not possess general
   host execution authority.
2. Core does not receive unrestricted Docker/Podman sockets.
3. Core does not mount arbitrary host or client project directories.
4. Core stores only authorized application state and bounded snapshots.
5. Client Agents independently authorize local workspace reads and future
   modifications.
6. Browser authentication does not confer device-local authority.
7. Sandbox execution is restricted to disposable, task-scoped environments.
8. The Sandbox Broker may control only its own authorized task containers.
9. Task approvals, policy checks and execution-target restrictions remain
   independently enforced.
10. Project changes cannot be automatically applied to client devices.
11. Uncertain operations must not replay automatically.
12. The existing legacy TUI remains separately supported under its original
   security semantics.

## Authority matrix

`—` means no authority; `metadata` means bounded authorized metadata only;
`conditional` means a narrowly authorized future capability, not ambient
authority. "Administer" is limited to the resource named in that column.

| Component | Read | Write | Execute | Authorize | Administer |
|---|---|---|---|---|---|
| Browser | Its authenticated Core views; chat transcript/activity and project/device/snapshot metadata. No snapshot source bytes or local files. | Chat prompts and allowed metadata actions through authenticated/CSRF-protected APIs. No project source writes. | Browser rendering only; no tool, shell, task or container execution. | Authenticates the operator to Core. An operator can explicitly authorize paired-device metadata and request a snapshot; this is not local consent or execution approval. | No Core host, client, Broker or runtime administration. |
| SynAI Core | Its authorized application state; validated immutable snapshots; configured legacy workspace identity/status metadata. | Private metadata, chat/history state, bounded upload staging and immutable snapshot objects. No arbitrary source-workspace mutation. | Own service logic and chat provider calls only; no distributed project code or general host commands. | Validates browser/device credentials and metadata actions. Cannot substitute for local consent or Broker policy/approval. | Its private metadata/snapshot storage and its own service lifecycle only; no client device or general container runtime administration. |
| Linux Client Agent | Locally registered directory only after local binding authorization; its protected local identity/config. Per-capture consent gates transmission, not preview reads. | Its own protected state and local receipts; future changes only after a separately reviewed local policy and explicit consent. Current version writes no project content. | Its own bounded scan, hashing and network client. No shell, build/test or project-code execution. | Local user authorizes the directory for scoped reads and separately approves each upload after preview. Browser/Core requests cannot create either authorization. | Its local credential/binding state only; cannot administer Core/Broker/runtime. |
| Sandbox Broker (planned) | Exact immutable task snapshot after authenticated task claim validation. | Only its own disposable task container/layer and bounded result objects. No client workspace writes. | Only approved task workload in task-scoped containers subject to independent policy/limits. | Enforces task claim, target, policy, approval, limits and cancellation independently; cannot create user approvals. | Only task containers it created and verifies as its own. No Core/TUI/foreign runtime objects. |
| Task container (planned) | Its explicit task input and task-private filesystem. | Only task-private disposable writable storage. No host/client workspace. | Task code within Broker-imposed limits. | None. | None. |
| Legacy TUI | Its selected conversation workspace, history and explicitly configured local execution environment. Phase 12 memory only if enabled and identity matches. | Workspace changes only through the existing dispatcher/backend and required approvals/policy. | Existing local Sandbox or explicitly configured HostExecution under the original TUI semantics. | Local interactive approval and Phase 10 policy. | Its conversation-owned sandbox only when ownership is verified; no Broker resources. |
| Legacy TUI Sandbox / HostExecution | Selected local workspace; sandbox container or host helper context as configured. | Authorized selected workspace per existing tool semantics. | Local coding-agent tools; HostExecution is explicitly not sandboxed. | No independent task/broker authorization; relies on TUI dispatcher, policy and approval. | Existing local session/backend objects only. |

The legacy TUI execution row is an explicit compatibility carve-out under
invariant 12; it must not be reused as evidence that Core or the planned
Broker has equivalent isolation.

## Data and identity boundaries

- Browser session tokens and CSRF secrets are distinct from device
  credentials and Ed25519 private keys.
- A logical project ID does not identify a filesystem path.
- A binding joins a logical project, paired device and local Client Agent
  association; it does not contain a server-resolvable local path.
- Snapshot identity binds a project, source device, binding, canonical
  manifest and content digests. Its files are untrusted source input.
- Legacy path-based project identity and Phase 12 memory namespace are not
  distributed logical-project identities.
- Broker allocation IDs and task claim IDs are not container IDs or
  independent proof of runtime ownership.
- Capabilities are descriptive inputs only. A feature string never grants
  authorization or proves that a device/target enforces the advertised
  behavior.

## Threat assumptions

- Network links may be observed, interrupted, replayed or redirected. Remote
  Client Agent transport requires HTTPS/WSS with normal TLS certificate
  validation. Device request authentication additionally binds body/path,
  timestamp and one-time nonce.
- Browser sessions may be stolen or abused; CSRF, Origin validation,
  HttpOnly/SameSite cookies, rate limits, expiry and revocation reduce but do
  not eliminate this risk.
- A paired device may be compromised. Revocation and binding checks limit
  access, but the client runs with the local user's filesystem permissions.
- Project source and model-facing source are untrusted and may contain
  malicious instructions, vulnerable code or secrets.
- A single-user Core deployment trusts its operator and process account.
  Compromise of that account/process compromises its authorized metadata and
  retained snapshots.
- A future Broker host/kernel/runtime compromise defeats container-level
  assumptions. Rootless Podman reduces authority; it is not a kernel
  isolation proof.
- Local file reads can race with other processes; no-follow/identity/digest
  checks and retries detect many changes but do not make the filesystem
  globally immutable.
- Storage protection assumes private ownership/modes and operating-system
  access controls. Disk theft, privileged host access and backups require
  separate protections.
- A single data-root owner process is assumed. Current shared web/TUI data
  root ownership is not a horizontally scalable multi-writer design.

## Known limitations

- Client path exclusions and preview are conservative heuristics, not
  complete secret detection or redaction.
- Browser authentication is single-user, not project-level multi-tenant ACL.
- Server-side WebSocket device responses rely on TLS and ordered framing;
  they are not separately signed by a Core signing key.
- The existing task table and identity contract do not establish a durable
  claim/fencing authority.
- No production Broker, rootless Podman identity, runtime policy, image
  digest allowlist or deployment manifest exists.
- Existing TUI Sandbox can use a configured container runtime and selected
  image, binds a local workspace read-write and creates with bridge
  networking; HostExecution explicitly runs outside a container. These are
  legacy local semantics, not Broker guarantees.
- The existing architecture does not guarantee atomic multi-file restoration
  or mutation; it reports per-file state and does not replay uncertain work.
- There is no distributed patch format, client-side application protocol or
  receipt implementation.

## Enforcement allocation

| Invariant | Primary enforcement point | Independent verification required |
|---|---|---|
| Core lacks host execution/runtime authority | Core package/deployment dependencies and mounts; no runtime socket/API | Inspect production process UID, mounts, sockets, capabilities and outbound ACLs. |
| No arbitrary project mount | Broker request schema and fixed mount policy; Core only stores snapshots | Broker-side path/mount rejection and runtime inspect tests. |
| Local read/write consent | Client registry, preview, explicit capture consent; future patch consent | Client tests for binding revocation, noninteractive denial, path races and consent replay. |
| Browser/device separation | Separate auth dependencies and credentials | Cross-namespace negative tests for every route/socket. |
| Task/approval/policy boundary | Existing local dispatcher for TUI; future Core/Broker claim protocol | Broker repeats all checks; tampered/stale authorization tests. |
| Task-scoped disposable execution | Future Broker container construction | Inspect namespaces, mounts, capabilities, network and resource quotas on real rootless runtime. |
| Ownership | Future Broker-created owner labels/ID ledger plus runtime verification | Reject foreign/replaced containers before every control action. |
| No replay | Existing recovery states and device snapshot behavior; future durable claim ledger | Crash injection around every side effect and lost acknowledgment. |
| No automatic patch application | No current path; future Client Agent must implement explicit gate | Negative tests proving browser/task completion cannot cause local writes. |
