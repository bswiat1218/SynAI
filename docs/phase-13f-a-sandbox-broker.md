# Phase 13F-A — Isolated Sandbox Broker

Phase 13F-A adds a separately launchable `synai-broker` process and its
standalone package under `synai/broker/`. It does **not** connect the Broker
to browser tasks, model-generated tool calls, or `CodingAgentRuntime`. The
Core web request path does not dispatch through the standalone local
container backend or receive Podman authority. Core task records remain
disabled until the follow-on integration has been reviewed.

## Reused architecture

The Broker is not a second coding-agent engine. Phase 13F-B is expected to
adapt the existing `ExecutionBackend` contract and reuse `CodingAgentRuntime`,
`Tools.call`, Phase 10 policy and per-operation approvals, plan validation,
verification, repair/review bounds, `TaskChangeTracker`,
`CheckpointManager`, and model-routing provenance. None of those paths is
changed or invoked by this package. The standalone local container backend
remains separate and is not a Broker fallback; direct host-execution code was
removed.

Dependency map: Phase 13C immutable snapshot store → exact task-scoped
retrieval authorization → Broker validation and private immutable staging →
Broker-owned writable task copy → fixed rootless runtime and typed operation
protocol → bounded result evidence. A later Phase 13F-B adapter can connect
the existing `ExecutionBackend`/`CodingAgentRuntime` orchestration while
retaining Core policy, approvals, verification, and checkpoint authority.
Core retrieval and live-lease control channels are implemented behind the
optional control listener. Their authorization source is currently the
explicit test-only persisted integration grant; production approval/task
issuance remains an acceptance blocker.

The source-of-truth flow remains Client Agent → Core immutable snapshot store
→ Broker. The Broker's `SnapshotRetriever` interface is exact-ID/digest/task
scoped and never accepts an archive or a source path. Snapshot validation,
private immutable staging, and creation of a separate writable workspace are
implemented in the Broker package. The Broker's mTLS Core client retrieves
only the exact authorized snapshot and revalidates live lease authority. The
Broker refuses preparation without the snapshot retriever and refuses every
operation without the live-lease validator. No direct Client Agent → Broker
path exists.

## Service boundary and protocol

ADR 0004's accepted Core–Broker authentication is mutual TLS. The earlier
HMAC-authenticated Unix-domain-socket implementation was an architectural
discrepancy and has been removed. Both listeners are private loopback HTTPS
listeners; the Broker requires a Core-issued client certificate, and the
Core control listener requires a Broker-issued client certificate. Neither
listener binds a public interface. The service identities, certificate
issuance, SAN/EKU constraints, CA custody, rotation, revocation, and live
handshake acceptance still require operator provisioning and verification.
No ADR amendment is proposed.

Every operation has a strict versioned request and an Ed25519 Core-signed
task claim. The Broker is configured with only the Core public key. Claims
bind task, project, snapshot ID and manifest digest, operation ID and
argument digest, deadline, target/profile, operation kind, plan/policy/
approval provenance, lease generation/fencing token, image digest, and fixed
resource profile. The Broker independently verifies signature, expiry,
registered task identity, immutable provenance, arguments, resource/image
binding, replay, and live lease through Core's authenticated control channel.
Approval provenance comes from the persisted authorization record; the
Broker does not treat a fingerprint alone as user consent. Claim issuance is
available only for explicitly gated integration tests. No production
task-claim endpoint exists.

The request body is limited to 2 MiB, claims to 16 KiB, task claims to 60
seconds, individual files to 1 MiB, snapshots to 500 files/64 MiB, paths to
512 characters/16 components, command argument vectors to 64 arguments/4
KiB, and output to the selected fixed profile ceiling. The Broker persists
claim, signature, replay identity, and operation state before dispatch. A
restart converts claimed/running operations to uncertain; it reports that
state to Core when the control channel is available and never replays the
operation. Errors use bounded structured codes and do not return runtime
stderr or exception details.

Core's optional Broker control listener is enabled only by
`SYNAI_CORE_BROKER_CONTROL=1`. Its integration-test authorization path
additionally requires `SYNAI_CORE_BROKER_TEST_MODE=1` and a separately
configured test token. It issues short-lived Ed25519 claims from persisted
task/snapshot/policy test authorization, maintains monotonic fencing and
revocation, validates the live lease, grants one-use exact snapshot
retrieval, and independently verifies bounded result bytes/digests before
persistence. It is not connected to browser task execution, model tool
calls, or `CodingAgentRuntime`. Production approval sourcing and phase
integration remain deliberately absent.

## Service identity and runtime

The process requires a non-root UID distinct from the configured Core UID.
Its private state directory, SQLite task/operation/replay/ownership ledger,
and task workspaces are owned by the Broker identity. Only this process
invokes the configured Podman executable. Core receives neither a runtime
socket nor a runtime-admin API. A Broker command is never passed to a host
shell: Podman is invoked with an argument vector, and approved container
commands use an argument vector through `podman exec`.

Runtime policy is fixed in Broker code: rootless Podman, a single operator-
configured `repository@sha256:...` image, non-root container UID 65532,
no added capabilities, dropped `ALL`, no-new-privileges, the configured
seccomp profile, `--network=none`, private IPC, no host PID/IPC/network,
read-only image root, a private bounded tmpfs, and one exact Broker workspace
bind mount. No caller-supplied mount/image/privilege/runtime option is
accepted. CPU, RAM, PIDs, wall time, and output have fixed ceilings. The
Broker never pulls images.

Startup checks rootless mode, cgroup v2, seccomp support/profile, user-
namespace mapping, an enforcing AppArmor/SELinux runtime, the local image
digest, private state, and a workspace quota mechanism. Any missing check
makes execution unavailable. The workspace-quota check is deliberately
false until an enforceable aggregate writable-storage limit has been
reviewed and implemented; counting files after a command is not an
enforcement mechanism.

The inspected host reports rootless Podman 5.7, cgroup v2, seccomp support,
subordinate UID/GID ranges, and a user namespace. It has no local test image.
Podman reports AppArmor disabled and SELinux disabled. No separate Broker
identity or approved test image was provisioned. The process must not run
under the current Core identity; the code does not create a service account.
There is no enforceable aggregate workspace byte/inode quota. Therefore the
current machine cannot pass Broker runtime readiness.

## Operations, state, and evidence

The Broker operation validator supports bounded workspace-relative read,
list, write, exact patch, delete, command, verification, and artifact-
collection contracts. Workspace file reads/mutations use no-follow
directory descriptors, reject symlinks and special files, and require exact
preimage hashes for replacements/deletes. Commands are an allowlisted
executable plus bounded argv/cwd, not shell text. The only writable host
tree is the private disposable task workspace; container commands are not
enabled until runtime acceptance.

Task lifecycle state is persisted as preparing, ready, running, cancelling,
completed, failed, interrupted, uncertain, cleaning_up, or removed. At
startup, reconciliation inspects only container IDs in the private
allocation registry. It verifies identity, image, mounts, labels, and fixed
runtime restrictions before stopping a verified running container. Missing,
changed, inaccessible, or ambiguous records are quarantined with durable
evidence; no runtime-wide list or cleanup is used. Uncertain commands are
never retried. This code path has unit coverage but has not been accepted
against a real container on this host.

Collection derives changed files from the actual staged working copy and
records bounded artifact bytes, preimage/postimage SHA-256 values, and a
checksum of the bounded change manifest. Core independently recomputes
artifact bytes, hashes, bindings, and manifest digest before storing the
result. Phase 9 `TaskChangeTracker`/checkpoint integration and patch
application are not implemented here. Nothing is applied to a client.

## Enablement and security boundary

### Operator provisioning procedure (not executed)

Provision in a disposable VM first. An operator must review the exact host,
image, policy, and service-unit changes before applying them:

1. Create a dedicated unprivileged `synai-broker` account with no interactive
   login and no membership in Core, administrator, or general container
   management groups. Allocate non-overlapping subordinate UID/GID ranges
   (at least 65,536 IDs) in `/etc/subuid` and `/etc/subgid`; verify rootless
   `podman info` under that identity.
2. Enable and verify the host's enforcing AppArmor or SELinux policy, seccomp
   profile, user namespaces, cgroup v2 CPU/memory/PID controllers, and an
   actual per-task plus aggregate byte/inode quota on the filesystem used by
   `SYNAI_BROKER_STATE_DIR`. Do not start the service unless write-time quota
   behavior (including sparse files and concurrent writes) is demonstrated.
3. Create a Broker-only state directory and rootless Podman storage/run
   directories with restrictive ownership and modes. Do not share Podman
   sockets, storage, or credentials with Core. Place Core and Broker
   certificates/private keys and trust roots in identity-restricted paths;
   define renewal, revocation, and compromise rotation procedures.
4. Review and build the disposable test image from a recorded source and
   reproducible build recipe, scan it, then load it through the approved
   operator process. Record the exact local manifest digest and configure
   the matching `repository@sha256:...` reference. Do not pull it from Broker
   startup or from a task request.
5. Configure the Core and Broker environment variables separately, enable
   only the private loopback mTLS listeners, and run readiness plus the
   adversarial integration suite in the disposable VM. Preserve test logs
   and runtime evidence for independent review.

The repository does not select an approved image digest: none is present on
the inspected host, and selecting/building/loading one requires operator
approval. No OS account, host policy, image, service, or runtime data was
changed by this implementation.

The FastAPI web application does not discover or call this socket. Its
execution-target contract continues to report `execution_available: false`.
The Sandboxes page now describes execution as unavailable rather than
claiming that no Broker package exists. Browser Chat remains tool-disabled,
the Client Agent remains read-only, and Phase 13F-B/13G/13H remain out of
scope.

## Remaining risks and acceptance work

The Linux kernel and container runtime remain in the trusted computing base.
Rootless Podman is not a defense against a kernel/container escape, a
vulnerable approved image/toolchain, or compromise of the Broker identity.
Source snapshot privacy filters are heuristic; no system can guarantee the
absence of secrets from user-approved source. Certificate issuance and key
custody require deployment-specific least-privilege review.

## Phase 13F-A.1 acceptance matrix

| Invariant | Enforcement | Test/evidence | Result |
|---|---|---|---|
| Core–Broker mTLS, no public listener | Loopback TLS listeners and mandatory client certs | TLS handshake with provisioned identities not run | BLOCKED |
| Signed, bounded, replay-resistant claims | Core authority and Broker registry | `tests.test_broker_authority`, `tests.test_broker` | PASS (unit) |
| Exact one-use snapshot grant | Core snapshot grant and Broker manifest/hash validation | `test_exact_snapshot_grant_is_scoped_one_use_and_digest_bound` and snapshot tests | PASS (unit) |
| Live lease, revocation, monotonic fencing | Core authority checked before and during command operations | authority unit tests; real long-running container revocation not run | BLOCKED (runtime) |
| Durable operation provenance and Core result validation | Core metadata tables and bounded artifact recomputation | authority tests | PASS (unit) |
| Uncertain side effects are not replayed | Broker operation registry and startup state transition | `test_restart_reports_durable_uncertainty_without_replaying_operation` | PASS (unit) |
| Owned-container restart reconciliation | Registry-scoped inspect and verified-stop path | Real Podman restart/tamper/loss tests not run | BLOCKED |
| Aggregate writable byte/inode quota | No enforcement mechanism implemented | `workspace_quota` readiness is always false | BLOCKED |
| Rootless isolation, LSM, network/resource limits | Fixed Podman options plus startup probes | No approved local image, separate UID, or enforcing LSM | BLOCKED |
| No host fallback or browser execution | No Core runtime socket/API; no task route wiring | Static architecture and existing regressions | PASS (code path; regressions pending) |

No skipped or mocked runtime test is counted as a security pass. Until every
mandatory implementation and runtime rows pass and an independent security
review accepts the boundary, execution remains unavailable. Current
classification: **NOT READY — SECURITY IMPLEMENTATION GAPS**.
