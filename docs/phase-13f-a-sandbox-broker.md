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

For a 16 GiB candidate volume, total role entitlements, the 8 GiB /
16,384-inode reserves, and 256 MiB / 4,096-inode operational allowance must
fit the observed usable capacity; live free capacity is
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

### HIGH-3 registry trust and operator recovery

Registry trust is reported separately by `/v1/ready`: state storage unverified;
storage verified but registry not opened; registry verified/recoverable;
registry unavailable; corrupt; identity/policy mismatch; recovery incomplete;
and recovery verified/complete. Diagnostics report authority availability,
unknown container identity, unresolved containment, and operator intervention
without returning task contents or credentials. Unavailable historical state
is an **unresolved host-containment concern**, not evidence that no containers
exist. Quota inspection does not establish database integrity or ownership.

Service startup never provisions or adopts a registry. It verifies the bounded
state role first, opens only an existing database, verifies its identity seal,
checks SQLite integrity/schema/references and task/container/slot bindings,
preserves existing migrations and uncertain-operation recovery, and reconciles
exact recorded containers. Readiness remains unavailable until reconciliation
completes and all other isolation gates pass. Quarantined slots, legacy tasks
without assignments, ambiguous allocations, or unverified containers keep
recovery incomplete. A successful observation or repeat recovery call cannot
clear a latched registry failure. Startup and reconciliation are single-flight.

Two small files live only in the same approved bounded state role:
`registry-identity.json` seals database device/inode, SHA-256 content, schema
version, and the approved configuration fingerprint; `registry-interrupted`
marks access that has not reached a verified persistence boundary. These are
**not an emergency ownership ledger**: they contain no container IDs, approvals,
or allocation history and cannot authorize recovery in place of SQLite.
SQLite remains the sole ownership authority. Each serialized connection uses
existing-only mode, full synchronous durability, integrity/reference checks,
and a checked WAL checkpoint. After close, the seal is atomically replaced and
fsynced; only then is the interruption marker removed and the directory fsynced.
Unknown or missing active WAL/SHM, unexpected sidecars at a closed boundary,
interruption markers, pending seals, replacement, or configuration changes
require operator intervention. Connections are short-lived; normal close
checkpoints and removes WAL/SHM, so their absence at a sealed closed boundary
is expected, not interpreted as lost state. Checksums/integrity checks impose
I/O cost; hashing is limited to the fixed 512 MiB state-file ceiling.

Registry access rechecks the state-role assurance before persistent writes.
SQLite, checkpoint, rollback, commit, filesystem, and seal failures revoke
execution readiness, stop new allocation and slot reuse, and leave containment
unresolved if its result cannot be persisted. Access and quota verification
use consistent lock ordering and reentrant verification; SQLite/registry-lock
waits are bounded to two seconds. The watchdog remains lifecycle-managed even
when startup refuses state, but cannot restore readiness. Filesystem I/O itself
can stall in the kernel; this code is not a proof of bounded physical I/O or
power-loss durability. No unbounded fallback directory is used.

**Operator-assisted procedure (requires separate approval; not automated):**

1. Keep execution disabled. Preserve the database, WAL/SHM/journal, seal,
   interruption marker, task roots, artifacts, and existing lab evidence;
   do not delete sidecars or clear markers to make startup pass.
2. Confirm dedicated service identity, real inspector freshness, bounded
   state storage, filesystem identity, quota policy, capacity, and permissions.
   Independently inspect database backups, integrity/schema, approved Core
   claims, task/operation/approval history, exact container IDs and labels,
   allocations, slot assignments, and reconciliation evidence.
3. If ownership cannot be established, retain unresolved containment and
   escalate to the operator. No prefix enumeration, broad cleanup, host process
   killing, or speculative container termination is authorized.
4. Obtain explicit approval for any restore, legacy adoption/migration, policy
   change, or separately authorized exact-container recovery. Before first
   provisioning, independently prove this is a new deployment, not lost history.
   The offline library initialization API requires explicit `storage_verified`
   and `provision`/`adopt_legacy` arguments; the service never passes provisioning
   or adoption arguments. This is not a readiness bypass and no production
   provisioning CLI, external authority/signing mechanism, or recovery tool is
   installed by this phase. A seal alone is not an operator approval or proof
   that historical state was complete.
5. Restart only after an approved recovery establishes authoritative SQLite
   history and coherent identity evidence. Require fresh storage verification
   and exact reconciliation again; no uncertain operation is replayed and no
   slot is automatically freed merely because a container stopped.

HIGH-3's automatic refusal paths are implemented and tested with disposable
SQLite databases, injected I/O/transaction failures, and fake runtimes.
Production recovery/provisioning approval tooling, authentic historical
baseline evidence, actual crash/power-loss durability, quota exhaustion under
the kernel, and real-container reconciliation remain acceptance blockers.
HIGH-4, HIGH-5, and existing-file project-ID follow-up controls are described below.
The seal is Broker-owned corruption/replacement detection, not protection
against a compromised Broker identity rewriting both database and seal.

### HIGH-4 descriptor authority and remaining external boundaries

Broker filesystem authority is now a pinned `Directory` object, not a
previously checked pathname. It retains trusted ancestor descriptors and
device/inode/kernel mount IDs, verifies ownership and permissions, and
revalidates the configured spelling against those identities at use boundaries.
Linux `openat2()` uses `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV` for children. Trusted absolute
ancestry is opened component-by-component without `NO_XDEV`, so reaching the
approved storage mount from `/` is allowed; children cannot cross into another
mount after authority has been anchored inside that filesystem. Unsupported
architectures, blocked/unavailable syscalls, symlinks, and inconclusive mount
identity are refused without a `Path.resolve()` or ordinary-open fallback.
The initial implementation supports Linux x86-64 and aarch64.

Security-sensitive inventory:

| Surface | Verification and use |
|---|---|
| Storage roles and quota slots | Fresh inspector binding, ext4/device/mount/project checks, pinned role inode/mount, protected ancestors, and private owned directory descriptors |
| Task creation | SQLite assignment checked again under the opened slot; exclusive descriptor-relative mkdir; inherited project identity checked before staging |
| Snapshot and workspace | Descriptor-relative exclusive files/directories; no pathname copytree or walk; immutable snapshot permissions applied through descriptors; partial staging evidence preserved on failure |
| File operations | Restricted child resolution, bounded owned regular files, preimage checks, pinned parents rechecked before atomic replacement |
| Artifacts | Descriptor traversal and bounded reads; no-follow artifact directory; exclusive descriptor-relative evidence files and fsync |
| Cleanup and release | Descriptor preflight rejects unsafe content; no symlink traversal; exact assigned root only; project and allocation rechecked; uncertainty quarantines rather than deleting foreign evidence |
| Inspector socket | Protected directory, O_PATH socket descriptor, actual SO_PEERCRED UID/GID/PID validation, and post-connect endpoint identity comparison before sending a request |
| SQLite and recovery evidence | Pinned state-role directory; no-follow database/seal/marker opens, pinned database inode, directory-relative seal replacement/unlink/fsync, existing-only SQLite open and post-use identity checks |
| Logging | Opened verified log-role descriptor; pinned file identity checked on emit; substitution refuses logging and invalidates readiness |
| Podman mount source | Descriptor-backed task workspace and exact allocation validation; CLI execution refused pending an accepted source-identity handoff |

The inspector connects through `/proc/self/fd/<socket-fd>` **in the Broker
process**, retaining the O_PATH descriptor until actual peer and endpoint
checks complete. Local disposable Unix-socket tests exercise these Linux
semantics. This is not an inspector implementation or proof of its privileged
deployment. Challenges, freshness, boot epochs, and inventory-generation
checks remain mandatory. Metadata verification is not a substitute for the
connected process credentials.

Podman CLI accepts a source pathname and resolves it independently. Passing
`/proc/self/fd/<n>` to that separate process does not transfer authority and
is not used. Matching container mount-source text cannot establish the actual
mounted inode. Creation, start, and exec therefore refuse with
`bind_source_resolution_unaccepted` until a separately reviewed runtime
interface and acceptance controls demonstrate identity preservation. The pure
argument builder is tested but does not invoke Podman. Exact-container stop,
inspection, and containment remain separate and are not disabled by this gate.
There is no configuration/test flag that enables this unaccepted boundary.

SQLite has a second external pathname boundary: Python's SQLite API does not
expose an identity-preserving dirfd/VFS handoff for the database and sidecars.
Its existing-only URI is rooted through the Broker's retained state-directory
descriptor; the database is checked before/after opening and before sealing.
Controlled parent/database-replacement tests require refusal and preserve
interruption evidence. These checks do **not** prove the installed SQLite VFS
never canonicalizes or reopens a pathname during a race. Service execution is
latched unavailable with `external_filesystem_resolution_unaccepted`; later
acceptance must establish a safe VFS/storage boundary rather than merely remove
the gate. No SQLite trust/seal schema or second ledger is introduced here.

Task-controlled code must never be able to rename authoritative slot/task-root
ancestors. Descriptor access prevents symlink/mount escape and detects observed
substitution, but ordinary Linux unlink/rename lacks atomic inode-conditional
semantics. Concurrent mutation by the trusted Broker UID or a host administrator
is outside a proven atomic cleanup guarantee. Kernel-backed dedicated-identity,
permission, mount-namespace, and runtime acceptance must establish that those
actors cannot race authoritative namespace changes. Unknown paths are refused;
no uncertain slot is released and no unrestricted pathname retry is used.
Task-root inode pins are process-local and allocation-scoped; restart still
requires fresh quota/ownership and historical snapshot/reconciliation checks,
not an assertion of inode continuity across an outage.

The tests use disposable directories/SQLite, controlled replacement barriers,
mocked quota/runtime evidence, and local Unix sockets. Only `openat2`, no-follow
resolution, descriptor identity, and local socket behavior are exercised against
the current kernel. No project quota, remount, privileged inspector, live Podman
mount, or power-loss acceptance is claimed. HIGH-4 remains **partially resolved**
pending the SQLite/runtime boundary and dedicated-host acceptance. HIGH-5 and
MEDIUM-1 are addressed separately below.

### CLI/watchdog lifecycle correction (A.5.1a.4.1)

The prior CLI called `asyncio.run(service.start())` before Uvicorn, placing the
watchdog on a temporary loop that closed before HTTP serving. This was a real
lifecycle defect. The CLI now only constructs the service and app and invokes
Uvicorn with lifespan explicitly enabled. FastAPI lifespan now configures verified
bounded logging before service startup diagnostics, then awaits startup and normal
registry reconciliation on Uvicorn's serving loop before admitting HTTP requests.
Its `finally` closes the
service, cancels/joins the watchdog, and waits for independent containment under
the existing bounded shutdown policy. Startup exceptions also run that cleanup;
failed storage/registry checks retain fail-closed diagnostics and never authorize
execution. No quota, inspector, identity, or runtime acceptance gate is relaxed.

One service instance owns one loop/lifespan. Concurrent application lifespans,
cross-loop reuse, and restarting a closed instance are rejected; a restart needs
a fresh service instance and normal verification/reconciliation. Unexpected
watchdog cancellation still revokes readiness before restarting monitoring on
the same live loop. Shutdown suppresses monitoring restart and marks the service
unavailable. Tests exercise the CLI-created application through ASGI lifespan
and a disposable loopback Uvicorn server, verifying idle ticks, expiry/failure,
startup admission barriers, shared loop identity, containment joining, and no
watchdog orphan after shutdown. These tests use fake storage/runtime adapters
and do not establish production inspector or real-container acceptance.
Shutdown also joins an in-flight inspector/filesystem worker before closing
registry descriptors: cancelling an asyncio waiter alone cannot stop a
`to_thread` operation. Tests use a controlled worker barrier to verify this
ordering. Inspector and registry waits retain their existing bounds, but an
underlying stalled kernel filesystem operation remains a deployment limitation;
this change does not claim that Python can forcibly terminate such a thread.

### Bounded runtime storage accounting (A.5.1a.5 / HIGH-5)

HIGH-5 is **partially resolved**, with code-level policy, observation, quota-domain,
and drift checks, but no complete version-qualified live runtime observer or
accepted launch-time pathname handoff. All real Broker runtime subprocesses,
including `info`, image inspection, exact-ID existence, stop, and removal, are
refused with `runtime_storage_acceptance_pending`. Even read-like Podman commands
can initialize storage. This is explicit refusal, not proof that an existing
container has stopped. HIGH-2 ownership/containment state handling is preserved;
unavailable runtime evidence remains unresolved and requires operator assistance.
HIGH-4 bind-source refusal is independent and remains unchanged.

The inventory distinguishes these growth-capable destinations:

| Destination/purpose | Approved domain or policy |
|---|---|
| graphRoot; images/extracted layers; writable layers; overlay upper/work/lower backing directories | `podman_graph`, 4 GiB / 131,072 inodes |
| Persistent libpod database, container static metadata, hostname/hosts/resolv files | Within the same graph quota, not an additional entitlement |
| Volume storage directory | graphRoot/volumes is accounted for; named/external volumes are unsupported |
| Rootless runRoot, XDG_RUNTIME_DIR, OCI configuration/runtime state | `podman_runtime`, at most 512 MiB / 16,384 inodes |
| Engine temporary/per-boot state | runRoot/libpod/tmp; actual persistence/tmpfs semantics remain acceptance-pending |
| Image download/copy/extraction scratch, effective subprocess TMPDIR | `image_scratch`, at most 512 MiB / 16,384 inodes |
| TMP/TEMP, cache, separately inventoried host temporary domain | `temporary`, 512 MiB / 16,384 inodes |
| HOME, XDG_CONFIG_HOME, approved configuration, Broker SQLite/WAL/seal/interruption evidence | `broker_state`, 512 MiB / 16,384 inodes |
| XDG_DATA_HOME, rootless network configuration metadata | Graph quota; networking remains disabled |
| Podman container logs and event logs | Explicit `none` drivers; nonempty log destination or alternate driver is refused |
| Broker service logs | Existing `broker_logs`, 512 MiB / 8,192 inodes |
| Task snapshots, workspaces, artifacts | Four independently assigned slots, each 256 MiB / 4,096 inodes |
| Container /tmp | Private ephemeral tmpfs, 64 MiB / 4,096 inodes per container, within the container memory boundary |
| Extra image stores, storage driver options/helpers, config modules, hooks, alternate paths | Unsupported; refuse rather than infer a boundary |

`RuntimeStorageLayout` is a strict typed observation contract, not a constructor
of evidence from configured paths. It requires every listed root purpose, exact
driver/logging choices, and approved configuration digests. The native storage
report verifier checks graphRoot, runRoot, imageCopyTmpDir, volumePath, driver,
configFile, options, and unexpected fields. Its `runtimePaths` member denotes
**supplemental observer evidence**, not an ordinary Podman info field. No production
adapter currently supplies that evidence; missing data cannot pass. Container
inspection separately checks upper/work/lower paths, merged-path spelling,
static/OCI metadata, log destination, volumes, and exact /tmp limits.
Merged overlay mount identity is not accepted from its spelling: live mounted
filesystem checks remain blocked. Native inspection formats/options also require
version-qualified acceptance; deterministic fixtures are not native runtime proof.

Every observed backing path is checked through the existing role opener and
`openat2` descriptor-relative access: fresh privileged-inspector UUID/mount/quota
identity, ownership, permissions, local project ID/inheritance, no symlink or
cross-mount traversal, and pinned device/inode/mount identity. Different `st_dev`
values alone never establish boundaries. Independent project domains on one
dedicated ext4 filesystem remain supported. Nested/overlapping roles and reused
project IDs are refused. A bounded tmpfs for rootless engine state, if required
by the accepted Podman version, needs separately authenticated byte/inode and
memory-budget evidence; the current ext4-only inspector contract cannot supply
that. No permissive tmpfs fallback is implemented.

Operator-approved configuration must exist as read-only bounded regular files
`runtime-storage.conf` and `runtime-containers.conf` under the state role.
`SYNAI_BROKER_STORAGE_CONFIG_DIGEST` and
`SYNAI_BROKER_CONTAINERS_CONFIG_DIGEST` supply their SHA-256 approvals. Files
are read through one descriptor, have pinned inode identity, and are compared
to a strict supported TOML policy. No files are provisioned automatically.
Approvals, aggregate capacity, and reserves are included in the HIGH-3 seal
fingerprint when runtime configuration is approved. Changing/adopting that
policy for historical state requires the separate operator recovery procedure;
the Broker never rewrites a historical seal to make new configuration pass.

Upstream precedence matters: storage configuration, STORAGE_DRIVER/STORAGE_OPTS,
CLI root/runroot/driver options, containers.conf system/user/drop-in files,
CONTAINERS_CONF_OVERRIDE/modules, HOME/XDG defaults, and TMPDIR can change
destinations independently. The Broker emits an explicit environment (never
merges the host environment), selects the two approved configuration files,
and excludes remote/runtime overrides. Strict TOML rejects additional stores,
driver options, mounts, hooks, and extra settings. This establishes configured
policy only: external Podman reopening those files and interpreting defaults
requires live acceptance and currently cannot authorize a subprocess.

Aggregate accounting counts each quota domain once:
`remaining = sum(hard_limit - observed_usage)`.
The minimum simultaneously observed available bytes/inodes must cover remaining
entitlements + reserve + operational allowance. Four full task slots and all
other maximum roles total 7.5 GiB / 221,184 inodes; with reserve and allowance,
the initial requirement is 15.75 GiB / 241,664 inodes of usable capacity.
Quota caps are never added to free capacity. Actual filesystem totals and
available capacity come from independent observations and local filesystem
checks, not the nominal volume size. A formatted nominal 16 GiB filesystem may
fail. The allowance is a conservative policy floor, not a measured guarantee
of sufficient production metadata overhead.

Once a complete observation exists, startup/admission, active-operation checks,
and the independent ASGI-owned watchdog check configuration and runtime evidence
again. Drift, missing observer, timeout, quota/path failure, or inconsistent
evidence latches loss and invokes existing uncertainty/quarantine/containment
handling. Workers are joined on cancellation before descriptor closure.
Without observation there is no active runtime authority; readiness explicitly
remains acceptance-pending. Later successful mocks cannot enable execution.

Tests use disposable files, controlled inode/symlink substitutions, synthetic
quota observations and fake runtime callbacks. They do not prove project quota
enforcement, overlay accounting, engine tmpfs lifecycle, real Podman log/storage
semantics, external configuration TOCTOU resistance, or kernel crash durability.
No host Podman command, container/image operation, inspector implementation,
storage provisioning, or MEDIUM-1 remediation was performed by the HIGH-5 change.

Reference semantics: [Podman global options/environment](https://docs.podman.io/en/latest/markdown/podman.1.html),
[storage.conf](https://github.com/containers/storage/blob/main/docs/containers-storage.conf.5.md),
and [containers.conf](https://github.com/containers/common/blob/main/docs/containers.conf.5.md).
Live acceptance must pin and verify the deployed versions rather than infer
their behavior from these current upstream documents.

### Existing-file quota verification (A.5.1a.6 / MEDIUM-1)

MEDIUM-1 is **partially resolved**: descriptor-controlled file operations now
verify actual per-file quota assignment, including preexisting files, but the
SQLite VFS boundary and real kernel quota enforcement remain unaccepted.
Startup adds `sqlite_file_quota_vfs_unaccepted` and never enables execution.
HIGH-4 and HIGH-5 refusal gates remain independent and unchanged.

The existing `Directory` authority carries a role-bound file verifier into its
child authorities. Production role openers install that verifier; a plain
directory descriptor, directory inheritance, or test fixture is not production
quota evidence. Before returning an existing or newly created file descriptor,
it validates:

- Regular-file type, dedicated UID/GID, one hard link, and safe permissions.
  State/config/log files are private; task-file read permissions remain compatible
  with immutable snapshots. Group/other write and special permission bits fail.
- Fresh independently inspected role UUID, mount, quota mode, exact byte/inode
  limits, and owner identity; local filesystem device/UUID and mount identity.
- `FS_IOC_FSGETXATTR` on the **file descriptor itself** supplies the project ID.
  Regular files do not require directory-only `PROJINHERIT` flags.
- No-follow, beneath/no-cross-mount resolution, pinned device/inode/mount identity,
  and matching current directory-relative pathname. `O_PATH` inspection uses a
  separately opened readable descriptor with matching identity for the ioctl.

`O_TRUNC` is refused before open on a quota-bound authority. Newly created files
are checked before writing any payload, and descriptor-backed writes are checked
again around continued use. Failure leaves uncertain new files/evidence intact;
there is no relabel, relocation, truncation, or repair procedure.
File-verification failures immediately revoke execution/slot authority and latch
the storage manager unavailable; later successful observations cannot clear it.
Narrowly reverified reads may still inspect evidence without restoring authority.
Startup and
periodic role verification perform a bounded recursive metadata inventory of
existing files, including audit/configuration files, manifests, artifacts,
recovery evidence, and logs. File inventory is bounded by the role inode ceiling
and a depth limit; physical filesystem/inspector latency still requires deployment
qualification. No new privileged inspector protocol or server was introduced.

SQLite database, WAL/SHM/journal files, identity seal, interruption marker, and
pending seal use the same authority. Existing-only database startup, historical
seal fingerprint, integrity/reference checks, full synchronous mode, checkpoint
checking, and directory fsync remain intact. Sidecars are verified before/after
SQLite open, before yielding the connection, and around checkpoint/close.
A sidecar never observed in the active lifecycle may legitimately be absent;
an observed active sidecar disappearing before checkpoint is unresolved. Closed
sealed boundaries still require sidecars to be absent. Confirmed absence retires
only the in-memory pathname pin, not historical SQLite ownership.

The pending seal and marker are verified after creation and before persistent
use. Atomic seal/workspace replacement verifies both source and any existing
target, performs descriptor-relative replacement, updates only that expected
in-memory pathname pin, then verifies the resulting file and fsyncs the directory.
Failure after replacement remains unresolved and preserves interruption evidence;
it does not reseal or discard history. Recovery/reconciliation and persistent audit
rows remain in the same checked SQLite database, not a new authoritative ledger.

SQLite's VFS can open/create/write a database or sidecar by pathname internally
before a Python post-check. These checks do **not** establish atomic file identity
or quota verification across every VFS syscall. A transient VFS-created file may
not be observable after close. The original HIGH-4 SQLite path race therefore
remains, with an explicit readiness blocker; there is no generic pathname fallback
and no claim of complete VFS protection. Descriptor-relative replacement also has
an external pathname-mutation interval; post-checks refuse uncertain identities
rather than proving an atomic identity-preserving rename under hostile mutation.

The CLI configures quota-verified persistent logging first within the ASGI lifespan,
before service startup can emit persistent diagnostics. Unsafe logging initialization
aborts the lifespan. The configured role/path/owner and all existing flat log entries
are checked before opening `broker.log`; bare path authority is rejected. Every
emission verifies the held file and current pathname, uses checked unbuffered
descriptor writes, and propagates quota/I/O failure after revoking authority.
Failures latch logging unavailable and cannot be cleared by later mocked success.
The lifespan closes the owned handler after watchdog/containment/service shutdown.
Bootstrap/service-manager stderr policy remains a separate deployment acceptance
boundary; no persistent fallback file or directory is opened.

Automatic log rotation is **not supported**. External renaming/replacement is
refused; existing rotated or temporary log files must pass the same project/type/
owner/link checks at startup and periodic inventory. Interrupted rotation never
causes automatic deletion, truncation, or adoption of uncertain files. Operator
recovery must preserve and inspect the state and log evidence before separate
authorization. Log quota exhaustion is not success and does not authorize a
fallback sink.

#### HIGH-5 capacity reconciliation (no cap changes)

The prior 15.75 GiB free-capacity entitlement calculation is arithmetically correct
at four maximum slots and the maximum existing role quotas:

| Quota domain | MiB hard limit | Inode hard limit |
|---|---:|---:|
| Four task slots (256 MiB / 4,096 each) | 1,024 | 16,384 |
| Podman graph, including images/layers/metadata | 4,096 | 131,072 |
| Podman runtime | 512 | 16,384 |
| Image scratch | 512 | 16,384 |
| Broker state | 512 | 16,384 |
| Broker logs | 512 | 8,192 |
| Temporary data | 512 | 16,384 |
| **Hard-quota entitlements** | **7,680** | **221,184** |
| Minimum reserve | 8,192 | 16,384 |
| Operational allowance | 256 | 4,096 |
| **Initial free-capacity requirement** | **16,128** | **241,664** |

16,128 MiB = 15.75 GiB = **16,911,433,728 bytes**. Aliased purposes within
graph/runtime/state roles are counted once, not as extra hard quotas. Host
temporary and image scratch are separate domains. Container `/tmp` is ephemeral
memory/tmpfs, not another persistent filesystem entitlement; live memory/tmpfs
accounting is still blocked. For used domains, the required free capacity uses
remaining hard entitlement (`limit - usage`), never adds caps to free capacity.

This is not approval of a nominal 16 GiB volume: independently observed filesystem
total bytes/inodes must also meet the configured aggregate total floor (default
16 GiB / 262,144 inodes), and the available-capacity check must pass after real
formatting, metadata, and usage. The overhead allowance is a policy floor, not
measured production sufficiency. Native runtime paths, per-boot/tmpfs behavior,
overlay charging, file quota enforcement, inspector freshness, and SQLite crash/
power-loss behavior remain acceptance blockers, not a HIGH-5 redesign here.

Deterministic tests use disposable SQLite/filesystem fixtures, synthetic project
IDs and mount observations, controlled replacement, and injected quota failures.
They do not modify quota assignments or prove live kernel quota charging.

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

### Phase 13F-A.5.1a.8: safety-transition reliability (F1/F2)

Safety-path error diagnostics use a narrowly scoped best-effort emission
boundary. Ordinary logger/handler exceptions cannot interrupt readiness
revocation, registry failure callbacks, quarantine classification, exact-owned
containment scheduling, evidence-failure handling, watchdog restart, or
shutdown cleanup. The verified persistent handler still raises and retains
its failure latch; direct logging operations do not become successful.
There is no alternate disk sink or diagnostic queue. The readiness response
includes only the last emission failure in structured in-memory diagnostics,
explicitly marked `durable: false`; emission failure also revokes readiness.
Cancellation and process-level termination exceptions are not swallowed.

Watchdog negative results, malformed evidence, and verification exceptions
all revoke admission and enter the existing storage-assurance-loss transition.
Verification runs under the assurance lock; quarantine and slow containment
run after it is released. Admission-time negative verification likewise
releases that lock before safety handling. Single-flight containment remains
task-scoped, and later successful observations do not restore readiness.

Failed quarantine persistence does not prevent a containment attempt where
existing exact-container policy permits it, but the runtime still independently
requires trustworthy ownership. Missing/untrusted registry identity never
authorizes speculative termination. Unpersisted quarantine/evidence remains
unresolved with operator escalation; no quota slot is released on that basis.
All runtime subprocess, bind-source, inspector, and SQLite VFS acceptance gates
remain unchanged. Deterministic regression tests do not prove live containment.

F3 local quota-authority latch consistency, F4 repeated-cancellation worker
ownership, and F5 whole-process deadlines are addressed by the subsequent
code-only phase below. F1/F2 did not implement those changes or a Rust inspector.

### Phase 13F-A.5.1a.9: authority and lifetime reliability (F3/F4/F5)

A persistent-file failure permanently revokes ordinary local quota/slot
authority on the manager instance. Corrected observations cannot repopulate
usable slots, verify roles for use, open ordinary role/task authority, or
authorize cleanup/release. A separate short failure-state lock prevents
concurrent observations from overwriting the latch or its first reason.
Readiness includes `quota_file_failure`. Diagnostic inventory inspection may
continue, but does not restore writable authority. Independently verified
containment eligibility remains distinct: a log/task-file failure alone does
not revoke otherwise safe containment domains; failure within a required
containment role does. A new manager still requires historical registry
identity and recovery checks; there is no reset API.

All Broker threaded verification calls use one explicitly tracked,
serialized authority-worker owner shared by service and runtime. The executor
future is shielded separately from its task, so even direct cancellation of
the worker task cannot make a running thread appear finished. Startup,
admission, active monitoring, watchdog and runtime configuration checks
shield the worker and join it despite repeated cancellation. The serialization
lock stays owned until thread completion. Shutdown has a single shielded
owner, refuses new workers, joins existing work, and only then disposes
registry descriptors. Cancellation is propagated after the necessary join.
Readiness reports in-flight workers and sticky shutdown uncertainty.

Python cannot stop a thread in uninterruptible filesystem I/O. After a
one-second verification/join observation window, the Broker reports uncertainty
and operator intervention while continuing to own/join the worker. This is
**not a one-second shutdown guarantee**: shutdown may remain pending
indefinitely, resources stay open, readiness remains false, and a restart on
the same service is refused. Future deployment requires independently
accepted process-level supervision/containment; forcibly ending the Broker
is not proof of container termination or durable recovery.

Runtime subprocess entrypoints share a child-lifetime adapter using an
absolute event-loop monotonic deadline. Spawn, both bounded output streams,
and process exit share that budget; EOF does not prove exit. Up to 500 ms
(at most one quarter of the budget) is reserved **within** the deadline for
exact-child kill/reap cleanup. This can time out work before the advertised
total interval, but never adds a cleanup interval on top of it. Timeout
results are not authoritative inspection or absence evidence.

Cancellation during spawn leaves the shielded spawn task explicitly owned;
when it supplies an exact child handle, only that child is killed. Unknown
spawn ownership, kill failure, or unconfirmed reap produces sticky unresolved
status and operator escalation, refuses further child launches, and preserves
necessary lifecycle ownership, including tracked pending spawn and reap tasks.
Readiness reports their counts without inventing a process identity.
Repeated cancellation cannot abandon cleanup.
Shutdown makes a bounded additional supervision pass and retains registry
resources if native child lifetime remains unresolved. A stuck spawn or
unkillable child cannot be guaranteed gone by Python; no arbitrary PID is
guessed and no runtime-wide cleanup is used. Exiting the CLI child never
proves that a task container stopped: exact-container verification remains
independent.

These adapters are reached only after the existing unconditional runtime
acceptance refusal. No production CLI test bypass, Podman inspection,
emergency command, create/start/exec, Rust inspector, or readiness enablement
was added. Tests use fake child adapters and disposable ordinary files,
not native kernel quota or container acceptance.

Latency scope: startup quota/configuration checks and periodic/admission
thread checks no longer block the event loop while their filesystem worker
is running. Other synchronous registry hashing/checkpoint/fsync, recovery
and verified diagnostic writes remain real possible event-loop blockers.
A local disposable 106,496-byte registry benchmark measured five ownership
checks at 66-70 ms (median 68.5 ms); this is not a worst-case capacity or
production guarantee. Slow-task debug messages also include fixture setup
and concurrent test overhead. Verification was not weakened for performance,
and broader serving-loop I/O isolation remains deployment qualification work.

### Phase 13F-A.5.1b: offline Rust inspector prototype

The standalone [quota-inspector prototype](../prototypes/quota-inspector/README.md)
is a bounded ordinary-user fixture harness, not an installed inspector, daemon
or privileged kernel backend. It compiles independently of the Python Broker.
The existing Python v1 client remains unchanged. Shared synthetic golden
fixtures and disposable Unix-socket tests check compatibility, actual connected
`SO_PEERCRED`, replay/freshness/epoch checks and malformed-input refusal.

Fixture observations never query the synthetic inventory paths, real devices,
quotas or mounts. The native backend remains explicitly unavailable. No
production configuration, CLI toggle, environment switch or startup wiring
selects this fixture harness. Protocol v1 success itself is **not**
cryptographically distinguishable from a genuine helper's response; it must
not be deployed at a trusted inspector endpoint. Isolation rests on the offline
test boundary plus the Broker's independent local mount/project-file checks,
historical registry verification, SQLite VFS refusal, bind-source identity
refusal and unconditional runtime subprocess acceptance guard. No fixture
success authorizes execution or proves write denial.

For future separately approved isolated-VM kernel work, resolve immutable
inventory paths descriptor-relatively with no-follow component traversal.
Check filesystem UUID/device correspondence and namespace-local mount IDs
before and after read-only observation. Verify actual project IDs on existing
files and directories separately from directory inheritance. A future
`quotactl_fd()` query must use a validated filesystem descriptor and report
kernel-observed configuration, limits and usage; empirical byte/inode
write-denial tests remain separate acceptance evidence. `CAP_SYS_ADMIN` is
broad administrative authority, not a quota-only privilege. Strict syscall
restrictions, enforcing AppArmor, privilege minimization and independent
review are prerequisites to any privileged experiment, not work performed
by this prototype. ext4 observations do not prove tmpfs, memory, cgroup,
Podman or container-resource enforcement. Native inspection, container
containment and post-crash supervision remain unaccepted.

Storage planning retains 7.5 GiB hard-quota entitlements, 8 GiB continuous
reserve, 256 MiB allowance, 15.75 GiB required initial available capacity
and 241,664 required available inodes. 24, 32, 64 and 128 GiB are planning
candidates only; **128 GiB is the planning maximum**, not an authorization
to provision or alter geometry, entitlements, reserves, mounts or quotas.

#### Phase 13F-A.5.1b.2: offline R1-R6 remediation

Response validation now distinguishes wire-field presence from an absent
optional Rust value. Status-specific exact schemas reject extra null fields,
missing keys and null mandatory values without changing Python v1. Shared
golden vectors cover success, unavailable and inconsistent responses. Canonical
generation uses Python's single encoding, including DEL, control characters,
quotes/backslashes and supplementary Unicode.

The fixture harness uses fixed-capacity rolling admission accounting rather
than retaining every refused connection attempt. Ten admissions per rolling
second include unauthorized/malformed connections; peer credentials are still
checked before reading payloads. The single-active-connection/backlog-four
boundary is unchanged.

The lifecycle budget starts at executable entry, includes startup, and bounds
nonblocking startup/diagnostic output and connection servicing. Ordinary errors
reach identity-checked owned cleanup; cleanup failures are unresolved errors,
not successful shutdown. Filesystem calls, scheduler stalls and SIGSTOP cannot
be forcibly interrupted by a self-enforced timer. The Python test supervisor
independently enforces monotonic incremental startup reads, bounded output
capture, exact-child termination/reaping and explicit uncertainty on cleanup
failure. A reaped child is not evidence of native quota or container acceptance.

Same-UID pathname races remain outside this offline trust boundary; inode checks
are not atomic check-and-unlink protection against a malicious same-UID process.
RAII cannot clean up after SIGKILL, abort or power loss. Disposable parent
cleanup is owned by the external tests, never an untrusted automatic sweep.
Production endpoint configuration can technically select any trusted compatible
helper, including a fixture socket; no fixture/genuine cryptographic distinction
was added to v1. Independent local filesystem, VFS, bind-source and runtime
acceptance gates remain active. No native backend or A.5.1c work is introduced.

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
