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
read-only image root, a private `/tmp` tmpfs limited to 64 MiB and 4096
inodes, and one exact Broker workspace bind mount. No caller-supplied
mount/image/privilege/runtime option is
accepted. CPU, RAM, PIDs, wall time, and output have fixed ceilings. The
Broker never pulls images.

Startup checks rootless mode, cgroup v2, seccomp support/profile, user-
namespace mapping, an enforcing AppArmor/SELinux runtime, the local image
digest, private state, and a workspace quota mechanism. Any missing check
makes execution unavailable. Workspace quota readiness is true only after
live ext4 project-quota state and the complete configured slot inventory
have been verified; it is not inferred from configuration or `df` output.

The inspected host reports rootless Podman 5.7, cgroup v2, seccomp support,
subordinate UID/GID ranges, and a user namespace. It has no local test image.
Podman reports AppArmor disabled and SELinux disabled. No separate Broker
identity or approved test image was provisioned. The process must not run
under the current Core identity; the code does not create a service account.
The current host has no provisioned dedicated quota filesystem or approved
Broker identity, so it cannot pass Broker runtime readiness.

## Phase 13F-A.5 — quota-aware task workspace slots

Broker configuration accepts an operator-defined inventory of at most four
pre-provisioned slots. Each has a canonical path, expected ext4 UUID, unique
project ID, and fixed byte/inode hard limits (at most 256 MiB and 4,096
inodes). The initial active-container limit remains one. Claims and operation
arguments cannot select paths, project IDs, or quota settings.

The operator inventories are supplied through `SYNAI_BROKER_QUOTA_SLOTS` and
`SYNAI_BROKER_STORAGE_ROLES`. Storage roles cover each task slot, Podman graph
and runtime storage, image scratch, Broker state, Broker logs, and temporary
storage. They require unique project-quota domains on one approved bounded
filesystem; separate physical filesystems are not required. The initial
policy caps are four 256 MiB / 4,096-inode task slots, 4 GiB / 131,072 inodes
for Podman graph storage, 512 MiB / 16,384 inodes for Podman runtime, image
scratch, Broker state, and temporary storage, and 512 MiB / 8,192 inodes for
Broker logs. At least 8 GiB and 16,384 inodes remain reserved. These values
are operator policy ceilings, not measurements of the current host. Aggregate
role entitlements plus reserves must fit both the configured filesystem size
and live available block/inode capacity.

Quota observation now uses protocol v1 over a local Unix-domain socket to a
future separately confined host inspector. The Broker sends only a configured
role ID, a random challenge, and the deterministic inventory generation.
The A.5.1b server contract must authenticate the connecting Broker UID/GID via
Unix peer credentials before parsing the request, and independently reject
any role outside its fixed operator inventory; no public or client-facing
socket is part of this interface.
Responses bind the role, challenge, protocol version, helper boot epoch,
inventory generation, observation time, filesystem/mount identity, quota
configuration, project ID, ownership, hard limits, usage, and filesystem
capacity. The client verifies the socket path and peer UID/GID, rejects
replay, stale epochs, stale responses, schema deviations, and oversized
frames, and uses bounded retries/timeouts. The response's quota-configuration
field is not evidence that the kernel rejected an over-limit write.

The Broker independently opens each configured path without following
symlinks and cross-checks the mount ID, ext4 UUID/device mapping, project ID
and inheritance attributes, `prjquota`/`pquota` mount state, directory
ownership/permissions, and local `statvfs` capacity against the inspector
response. It requires zero effective capabilities and no extra service
groups. Direct `quotactl(Q_GETQUOTA)` calls have been removed from the
unprivileged Broker. The privileged inspector is not implemented in this
stage; until it is installed and independently reviewed, all storage
inspection is unavailable and Broker readiness remains false.

The SQLite registry transactionally binds each task and allocation to one
fixed slot using a collision-resistant private ownership token. Slot states
are `free`, `allocated`, `release_pending`, or `quarantined`. Restart marks
in-flight tasks uncertain and quarantines their slot; it never moves task
files or reuses an uncertain slot. Removal enters a durable release-pending
state, and the slot returns to `free` only after operation evidence, empty
workspace verification, and (when a container existed) independent Podman
confirmation that its owned container is absent. Ambiguity quarantines the
slot. Historical tasks without assignments are preserved and block new
workspace readiness rather than being silently relocated.

All persistent task files, snapshots, writable workspaces, manifests, and
collected evidence are resolved through the durable task-to-slot assignment.
Podman graph storage (including image layers and container writable layers),
Podman runtime storage, image scratch, Broker SQLite/WAL state, logs, and
temporary paths each have separate project-quota domains on the same bounded
filesystem. Podman `HOME`, `TMPDIR`, and `XDG_RUNTIME_DIR` point into the
inventoried bounded roles. Container `/tmp` is a private tmpfs capped by both
bytes and inodes. Uvicorn service logs go to the private `broker.log` within
the inventoried log project quota, and HTTP access logging is disabled. A
different `st_dev` value is not treated as proof of a
hard bound; a root-backed or otherwise unbounded role fails inventory checks.
External service-manager logging policy and helper confinement still require
deployment acceptance.

After startup has opened the verified registry and completed reconciliation,
the Broker runs one independent storage-assurance watchdog. Its default
interval is one second and can be configured with
`SYNAI_BROKER_STORAGE_WATCHDOG_INTERVAL_SECONDS`; it must remain below the
inspector evidence maximum age. The watchdog serializes its checks with
operation admission, covers idle periods, and applies to every operation
class. Missing, expired, inconsistent, or unavailable evidence immediately
revokes Broker execution readiness. A watchdog exception also revokes
readiness and is logged; it is not treated as a successful check.

Readiness is deliberately latched unavailable after assurance loss. A later
successful periodic observation cannot restore it: a service restart must
reopen the verified registry, complete recovery/reconciliation, and pass all
startup gates before readiness can return. If state-role verification prevents
registry initialization, the service refuses startup recovery and creates no
replacement database or fallback journal. Shutdown cancels and joins the
watchdog before closing the Core client. The Python watchdog and fake-runtime
tests do not establish privileged-inspector freshness, kernel quota
enforcement, or real-container containment.

For a 16 GiB candidate volume, total role entitlements and the 8 GiB /
16,384-inode reserves must fit the observed capacity; live free capacity is
rechecked at startup and before operations. Each task's snapshot, writable
copy, manifests, and artifacts consume the same per-slot project quota. The
initial one-active-container limit remains in force.

When storage inspection is lost or inconsistent during active work, the
Broker immediately revokes execution readiness, blocks new allocations, and
durably marks operations uncertain and assigned slots quarantined where the
registry remains writable. Emergency containment is separate from execution
readiness and quota-inspector availability: it may use only the independently
restricted Broker runtime authority and a readable authoritative ownership
record. The record must bind the exact container ID, task, allocation, pinned
image digest, snapshot digest, and resource profile; Podman inspection must
also confirm the exact ID, expected labels, and previously verified image ID.
Missing or inconsistent evidence is unresolved and never authorizes a
termination command.

For an eligible container, the Broker inspects state, requests a bounded
graceful stop, inspects again, and requests a bounded exact-ID forced stop
only if still running. It inspects once more and records `verified_stopped`
only from matching runtime evidence. `verified_absent` requires an exact-ID
Podman existence query to confirm absence; runtime errors are not absence.
Still-running, unavailable, mismatched, or inconclusive states remain
unresolved, keep readiness unavailable, and require operator intervention.
The containment operation is single-flight per task and shares a lock with
start, normal termination, and removal. It never releases a quota slot or
performs runtime-wide cleanup; removal and slot reuse remain subject to the
separate cleanup and reconciliation gates. Container artifacts and task files
are preserved.

If registry ownership records cannot be read, the Broker does not infer that
no container exists and does not choose a target through Podman enumeration.
It refuses containment and escalates to operator-assisted recovery. Durable
quarantine or containment is claimed only when the registry write succeeds.
This is the HIGH-3 registry-loss recovery boundary: no independent recovery
journal or root-filesystem fallback is introduced here.

These controls are unit-tested with fake runtime and registry adapters only.
They do not establish real Podman stop/kill behavior, privileged-inspector
availability, kernel quota enforcement, or production containment acceptance.

Collected change manifests (including bounded artifact payloads) are fsynced
under the assigned task root before Core result recording. The Broker's
SQLite operation audit stores only the result digest/size and a slot-relative
artifact reference, not a second copy of task file content.

This is a fail-closed implementation, not proof of production enforcement.
No host quota, identity, service, image, or policy was provisioned by this
change. Unit tests use deterministic inspectors and do not count as kernel
quota acceptance. An explicitly opt-in write harness is available at
[`scripts/verify_broker_quota_lab.py`](../scripts/verify_broker_quota_lab.py);
it requires `--confirm-write-test`, only writes below an empty operator-
selected quota slot, and deliberately leaves its generated evidence in
place. The prior loopback lab result remains development evidence only.

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
   profile, user namespaces, cgroup v2 CPU/memory/PID controllers, and actual
   per-task plus aggregate byte/inode quotas for every configured storage role
   on a dedicated bounded filesystem. Roles may use distinct project IDs on
   that filesystem; physical filesystem separation is not required. Keep all
   roles off an unbounded host-root path. Do not start the service unless
   write-time quota behavior (including sparse files and concurrent writes)
   is demonstrated and the A.5.1b inspector has passed independent review.
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

Phase 13F-A.2 host findings, storage design, mTLS handshake evidence,
operator provisioning/rollback procedure, and reviewer package are recorded
in the
[Phase 13F-A.2 qualification package](./security/phase-13f-a2-qualification.md).
That package is not an independent security review or runtime acceptance.

| Invariant | Enforcement | Test/evidence | Result |
|---|---|---|---|
| Core–Broker mTLS, no public listener | Loopback TLS listeners and mandatory client certs | Disposable loopback TLS test; production identities/route unprovisioned | BLOCKED (production) |
| Signed, bounded, replay-resistant claims | Core authority and Broker registry | `tests.test_broker_authority`, `tests.test_broker` | PASS (unit) |
| Exact one-use snapshot grant | Core snapshot grant and Broker manifest/hash validation | `test_exact_snapshot_grant_is_scoped_one_use_and_digest_bound` and snapshot tests | PASS (unit) |
| Live lease, revocation, monotonic fencing | Core authority checked before and during command operations | authority unit tests; real long-running container revocation not run | BLOCKED (runtime) |
| Durable operation provenance and Core result validation | Core metadata tables and bounded artifact recomputation | authority tests | PASS (unit) |
| Uncertain side effects are not replayed | Broker operation registry and startup state transition | `test_restart_reports_durable_uncertainty_without_replaying_operation` | PASS (unit) |
| Owned-container restart reconciliation | Registry-scoped inspect and verified-stop path | Real Podman restart/tamper/loss tests not run | BLOCKED |
| Aggregate writable byte/inode quota | Fixed project-quota slots, bounded role inventory, typed inspector client, durable allocation | Broker quota-slot/protocol tests; no privileged inspector or production filesystem/identity | INTERFACES IMPLEMENTED; BLOCKED (kernel integration) |
| Rootless isolation, LSM, network/resource limits | Fixed Podman options plus startup probes | No approved local image, separate UID, or enforcing LSM | BLOCKED |
| No host fallback or browser execution | No Core runtime socket/API; no task route wiring | Static architecture, Python suite, frontend regressions | PASS (code path and regressions) |

No skipped or mocked runtime test is counted as a security pass. Until every
mandatory implementation and runtime rows pass and an independent security
review accepts the boundary, execution remains unavailable. The A.5.1a
protocol and storage-role policy are code-only interfaces; the privileged
inspector and real rootless-container acceptance are still absent. Current
classification: **NOT READY — INSPECTOR AND PRODUCTION ACCEPTANCE BLOCKED**.
