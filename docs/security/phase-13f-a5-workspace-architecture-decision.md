# Phase 13F-A.5 isolated task workspace design

**Status:** Option B is approved as the target for **offline architecture design
only**. This document is a design, not implementation or runtime acceptance.
The authorization does not permit Podman invocation, mount/namespace operations,
quota syscalls or changes, VM provisioning, container execution, service
installation, production readiness changes, or client patch application.
Execution refusals remain unchanged.

This design extends the existing Phase 13F-A.5 architecture and 13-row
[acceptance matrix](phase-13f-a5-acceptance.md). It does not create a second
task ledger or alter the existing registry schema.

## Decision and boundaries

Use immutable snapshot → authenticated Broker retrieval → bounded task staging
→ isolated, per-task Podman storage → task-rooted artifacts. Do not give a
container a direct bind mount or named volume backed by the client project,
Core snapshot store, or Broker's canonical workspace.

The selected import/export mechanism is **Podman's supported `cp` operation
against a non-running container**, with Broker-owned, descriptor-pinned staging
on the host. A fixed task image supplies an empty `/workspace` directory.
Validated files are copied into the container root filesystem before any
untrusted process starts. Export is performed only after exact-container
termination and process reaping, by opening the verified task root filesystem
and walking `/workspace` with descriptor-relative, no-follow traversal. Podman
`cp` may be used as a bounded fallback only if source review proves its
destination behavior safe and independently testable. No bind source, volume
source, container-supplied path, or `--preserve-fd` claim is used as authority.

The actual container root filesystem is still a runtime-created mount. Before
start, a trusted verifier must independently pin and validate the rootfs mount
and workspace directory, and establish that its writable overlay upper/work
storage is charged to the exact task quota domain. It must also prove that the
verified rootfs instance remains the one used by start, with no unverified
unmount/remount or path re-resolution between verification and workload start.
The `podman mount` pathname or `podman inspect` output alone is not proof.

This is a **conditional target**. If the selected pinned Podman version cannot
provide a documented and source-reviewable non-running import, exact rootfs
verification before workload start, continuous identity through start, and
complete writable-storage attribution, the result is **NO-GO**. Do not fall
back to a bind mount, a named volume, shared unbounded `graphRoot`, or weaker
pre/post pathname `stat`; return for a new architecture decision.

### Existing implementation to reuse

- Core's `SnapshotStore` commits device-authenticated immutable snapshots and
  canonical manifest digests. Broker snapshot validation already enforces
  regular UTF-8 files, path/size/count bounds, and content digests.
- Broker task claims already bind task/project/snapshot IDs and digest,
  operation/deadline, policy/approval provenance, lease generation/fencing
  token, image digest, and resource profile. Reuse the existing authentication,
  replay, lease, and task-registry authority; add no alternate claim source.
- Broker `Directory`, `open_beneath`, and snapshot helpers provide prior art for
  retained directory descriptors, path validation, and bounded traversal.
  Harden/reuse them as necessary; pathname `Path` values are diagnostic or
  external CLI arguments, never final authority.
- The current task registry remains the sole durable ownership/lifecycle ledger.
  The Core snapshot store remains the immutable source of truth. Artifacts are
  retained through the existing task artifact/result path.
- Keep the existing launch refusal until every applicable GO gate in this
  document and the matrix has independently passed.

## Dependency-ordered design and implementation plan

### 1. Bind the request to a current immutable snapshot

1. Core issues a short-lived, single-purpose snapshot retrieval authorization
   over the existing authenticated Core–Broker channel. It binds exact task ID,
   logical project ID, snapshot ID and digest, source generation, allocation,
   operation ID/deadline, policy/approval provenance, current lease generation
   and fencing token. It authorizes reading that snapshot only—not browsing
   Core paths or selecting a source.
2. Broker resolves the snapshot only through the Core snapshot API. It rejects
   caller-supplied filesystem paths, URLs, arbitrary project selectors, stale
   generations, expired claims, replays, mismatched digests, and a renewed or
   superseded lease.
3. Core returns the canonical manifest and content in bounded chunks. Broker
   authenticates every response, checks exact manifest schema and digest,
   verifies each chunk hash and final file SHA-256, and binds completion to the
   same claim/fence. No direct Client Agent → Broker data channel is introduced.
4. The source snapshot and Core's historical task/registry evidence are never
   modified by import, task execution, export, quarantine, or cleanup.

**GO:** exact snapshot identity, full content, generation, lease and authorization
are independently revalidated before and after transfer; incomplete, stale,
replayed, contradictory, or oversized transfer fails closed.

### 2. Enforce strict file and transfer bounds

Reuse the current snapshot limits as initial hard upper bounds; do not silently
raise them:

| Limit | Initial maximum |
|---|---:|
| Regular files | 500 |
| Per-file logical bytes | 1 MiB |
| Aggregate imported logical bytes | 64 MiB |
| Relative path length | 512 characters |
| Relative path depth | 16 |
| Directories | 500 |
| Snapshot tree inodes including root, manifest, files, and directories | 1,002 |
| Chunk transfer | 256 KiB |
| In-flight task operations | One per task/fencing generation |

All values are hard ceilings; a lower policy/profile limit wins. Existing
resource-profile time, CPU, RAM, PID, output, and storage bounds also apply.
Storage reservations include the imported copy, writable overlay upper/work,
task metadata, scratch, export staging, and cleanup reserve; copying does not
make physical capacity free.

For source and export trees:

- Accept only canonical NFC relative paths; reject absolute paths, `..`,
  empty/dot components, backslashes, controls, duplicate/case-fold collisions,
  and a file used as an ancestor.
- Accept only regular files and directories. For regular files require
  `st_nlink == 1`; reject symlinks, hardlinks, devices, FIFOs, sockets, mount
  crossings, sparse/extents beyond the allowed logical/allocated policy, and
  unknown metadata. Count all task metadata and temporary inodes against the
  enforced quota in addition to the snapshot-tree cap.
- Do not extract an untrusted archive. Create directories/files through a
  retained root descriptor and component-wise `openat2`/no-follow operations;
  create files exclusively, with bounded sizes and normalized modes. No source
  uid/gid, setuid/setgid/sticky bits, ACLs, xattrs, capabilities, or ownership
  metadata are imported.
- Detect sparse files by comparing logical size and allocated blocks/extents;
  enforce both limits. The incoming snapshot transfer is byte-streamed into
  newly created files, never copied with hole-preserving semantics.
- Count every inode, including directories and importer metadata. Include
  temporary and partial files in quota and output accounting. Reject a limit
  breach before publication; never truncate a successful-looking manifest.

The current snapshot contract already restricts source entries to regular
UTF-8 text with no NUL bytes. Preserve this restriction unless a separately
reviewed protocol version deliberately changes it.

### 3. Stage into a private, transactional task tree

1. Allocate the durable task record and fencing generation in the existing
   registry before filesystem creation. Its record binds project/snapshot
   identity, digest, generation, image/resource profile, exact storage-domain
   identity, and operation IDs.
2. Open the task parent and all ancestors by retained descriptors. Validate
   ownership, modes, filesystem and mount identity, and expected task
   allocation. Caller-provided paths are never authoritative.
3. Create a unique task staging tree in `IMPORTING` state. Keep it inaccessible
   to task processes. Use an importer identity with no untrusted process sharing
   its UID. Write into a fresh `.partial` tree; synchronize files/directories as
   required by the persistence contract; re-read and hash every file; verify
   exact entry set, byte/inode counts, and manifest.
4. Publish the immutable input and mutable host-side handoff with a same-
   filesystem atomic rename only after all checks succeed. The immutable source
   copy is never made writable. If atomic publication is unavailable, retain
   partial state and fail closed.
5. Record the committed import state and exact tree identity in the existing
   task ledger before advancing. Never infer successful import from directory
   existence, marker file, Podman label, or a matching approval fingerprint.

Task output is collected into a distinct Broker-owned export staging tree. The
task cannot access it. The host-side workspace copy is not mounted in the
container and is not the container's writable root.

### 4. Assign and verify dedicated quota-controlled task storage

Each task receives a distinct project-quota domain on the operator-selected,
qualified filesystem. Provisioning and quota changes are outside this
authorization. The later operator/VM setup must establish project quota
accounting, unique project ID allocation, directory project ID, project
inheritance, hard byte/inode ceilings, and behavior for exhausted quotas.
Broker must not infer any of these from configured path text or a successful
mkdir.

Before container creation, the quota authority records the task's exact
project ID and expected filesystem identity. A narrow trusted storage helper
may assign/verify the ID only if separately authorized and reviewed. It must
not expose general filesystem, quota, or runtime administration to Core or
task processes.

For every authority-bearing directory/file descriptor, independently check:

- `st_dev`, inode, type, owner, link count, mode, mount ID, filesystem type,
  filesystem UUID, and namespace correspondence against operator-provisioned
  inventory and retained descriptors.
- Filesystem project ID and project-inheritance flag from the kernel on the
  actual task root and each relevant descendant/domain. Verify live quota
  usage, hard byte/inode limits, reservation, and free reserve through the
  approved read-only authority. Configured values are not observations.
- Directory ancestry and task-parent identity; no untrusted mount, symlink,
  hardlink, replacement, or alternate mount can enter the domain.

The complete writable-domain inventory must include, at minimum:

1. Per-task Podman graph root: container upper, overlay `work`, writable
   container metadata, extraction/copy temporaries and any writable image
   layers.
2. Per-task run root: runtime state, sockets, PID/state files, and temporary
   data.
3. Per-task Broker staging/import/export/partial/quarantine trees and any
   snapshot copy retained for the task.
4. Container writable rootfs paths, `/workspace`, `/tmp`, home, caches,
   package/compiler scratch, and every writable tmpfs or anonymous memory
   filesystem, with independent byte/inode/memory caps where project quota
   does not apply.
5. Podman logs, events, lock/state files, runtime scratch, OCI bundle, helper
   output, crash dumps, and any other path observed in the exact pinned
   configuration and runtime mount table.
6. Shared read-only image layers and shared Podman/Broker metadata: establish
   immutability and account shared consumption once in their own approved
   domain; no double-counting or omission. Prefer per-task graph/run roots so
   writable task state does not fall into a shared `graphRoot`.

The existing `RuntimeStoragePolicy` role inventory and aggregate quota rules
must be extended only by an explicit reviewed change. Preserve existing
entitlement, reserve, and **128 GiB absolute planning ceiling**. Do not treat
per-task maxima as permission to increase those totals.

**GO:** every writable path and backing mount is enumerated from independently
observed runtime state, mapped to an enforced project quota or a separately
bounded non-project domain, and shown to remain within the exact task allocation.
Unknown paths, shared writable domains without attribution, unsupported
project-quota inheritance, missing UUID/mount evidence, or quota ambiguity mean
NO-GO.

### 5. Use Podman rootfs staging, not a mounted host workspace

**Candidate interface under investigation:** pinned-version rootless Podman
CLI/API: create a container from the approved image digest in an isolated
per-task graph/run root; transfer only validated entries with Podman's
container-copy operation (`podman cp`/equivalent documented API) into the
fixed `/workspace`; ask Podman to expose that exact container root filesystem
for trusted inspection; then start the same recorded container. Export uses a
separate rootfs inspection after exact workload termination.

**Installed 5.7.0 evidence and current verdict:** the installed `podman-cp(1)`
manual says containers can be “running or stopped,” but does not explicitly
define whether a never-started `created` container is accepted by the copy
path. It documents path-valued operands and tar-stream input/output; local
symlink sources are followed by default, and rootless copy from a running
container may ignore permission errors. The staged importer must therefore
provide only private, validated ordinary files and must not treat copy success
as verification. No Podman source package is installed here; only the packaged
5.7.0 man pages were inspected, without invoking Podman.

The installed `podman-mount(1)` describes mounting a working container's
rootfs and says rootless mounting works only from inside the user namespace.
It prints a path; the manual does not claim the path is an authenticated
descriptor, establish project-quota identity for overlay upper/work, or
guarantee that a mount verified before start is the exact mounted object used
by the workload. `podman-unmount(1)` documents a mount counter and warns that
forced unmount can disrupt other processes. These interfaces provide no
documented pre-start lifecycle barrier or identity-continuity guarantee.
Kernel inspection of a pinned returned path could identify the currently
mounted object, but cannot alone prove its binding to the container storage
record, charged upper/work domain, or the mount actually consumed after a
separate start operation.

**Consequent status: NO-GO for modeled runtime start pending source/VM
acceptance.** The fake lifecycle below can exercise ordering and refusal
contracts only. Podman copy to a created-but-never-started container, exact
rootfs exposure, upper/work attribution, and identity continuity through start
remain unproven. Do not implement a real adapter or change the runtime launch
refusal on this evidence.

This is a staging/copy boundary, **not descriptor-authoritative input**.
Podman's copy command receives path-valued operands. The Broker therefore gives
it only a private, retained, non-writable handoff tree under Broker-only
ancestry, with no untrusted same-UID peer. Validate the exact pinned version's
copy semantics and limits from authoritative docs/source before relying on it.
Do not pass an untrusted archive and do not rely on `--preserve-fds` as a
mount-source mechanism.

The per-task Podman invocation must use the exact configured executable and
approved config, storage root, run root, pinned image digest, non-root
subordinate-ID mapping, no network, no added capabilities, no-new-privileges,
approved seccomp/LSM, read-only base image, and explicit resource limits.
Never use `keep-id` if it maps the task process to the Broker/operator host UID.
Image pull, build, dependency installation, arbitrary volume, host bind, host
socket, device, hook, or runtime option is prohibited. A preapproved local
image must be imported/verified without network and included in storage
accounting.

**Pre-start verifier contract:**

1. Resolve the exact container from the registry's recorded full container ID,
   not labels alone. Confirm task ID, allocation, image digest and generation
   against the durable task record and current lease.
2. Have Podman expose the rootfs of that non-running container. Acquire a
   descriptor to the returned root and inspect `/proc/self/mountinfo`, fdinfo,
   `fstat`, `fstatfs`, the pinned mount namespace, and the overlay mount's
   actual upper/work backing descriptors. The textual mount path is only a
   locator; open it beneath the already trusted runtime root and pin every
   result.
3. Verify that `/workspace` exists as a directory beneath the pinned rootfs,
   is not a symlink/mount escape, is on the expected rootfs mount, and contains
   exactly the validated manifest entries. Re-read each file by descriptor and
   check type, owner mapping, link count, normalized mode, size and digest.
4. Independently inspect upper/work and all other writable paths; verify
   filesystem UUID, mount ID, project ID, inheritance, quota and resource
   ceilings, plus the container's user namespace mapping. No runtime-
   supplied label or container-provided metadata substitutes for kernel
   observations.
5. Maintain a trusted lifecycle barrier from successful verification through
   start. The implementation must prove the mount/rootfs is not detached,
   substituted, or recreated between inspection and the first untrusted
   instruction. Use an exact supported runtime pre-start lifecycle facility
   only if its pinned-version semantics and authority are established. A
   separate `podman mount` inspection followed by an uncoordinated `podman
   start` is not sufficient by itself.
6. Persist the verified identities and transition to `READY_TO_START` in the
   existing ledger before start. Any mismatch, lost descriptor, expired lease,
   restart ambiguity, or failed observation aborts and quarantines; it never
   falls back to a pathname bind.

The Podman copy/rootfs sequence above remains the only design candidate, not an
accepted implementation interface. Its created-container copy semantics,
mount lifetime, quota attribution, and pre-start identity continuity have not
been proven for a pinned Podman/OCI implementation. They are mandatory
source/documentation questions before any real adapter and mandatory
isolated-VM acceptance before execution. If any answer is unavailable, retain
the runtime workspace NO-GO and launch refusal; request a new decision rather
than implementing a weaker path. A named volume is not an alternate path unless
it independently meets the same proof.

### 6. Separate trusted identities and defeat same-UID substitution

- Run Core, Broker control, and any narrow privileged storage helper under
  distinct dedicated service identities. No service shares Core's credentials,
  history, project state, registry, socket, or writable mount with a task.
- Broker task directories are private, descriptor-pinned, and inaccessible to
  Core clients and task UIDs. The Broker service UID has no interactive login
  or unrelated process sharing. Any helper has only the specific verified
  storage operation and cannot run arbitrary commands.
- Map task processes to allocated subordinate host UIDs/GIDs distinct from the
  Broker/operator/service UIDs. Verify `/proc/<pid>/uid_map`, `gid_map`, groups,
  capabilities, no-new-privileges, seccomp, LSM, namespaces, and exact process
  identity before accepting a start. Refuse `keep-id` or any mapping that makes
  the untrusted workload share the Broker host UID.
- Retain root/task directory FDs and use descriptor-relative lookup, exclusive
  create, no-follow resolution, single-link checks, and post-I/O identity
  checks. The task never receives those host FDs. FD passing to a workload
  grants only the explicitly documented data capability; it is never claimed
  to authorize a mount.
- **Same-UID limit:** DAC, a private mode, a dirfd, or pre/post inode comparison
  cannot defend against an arbitrary malicious process with the same effective
  UID. The security boundary is that no untrusted process shares that UID. If
  the deployment cannot enforce and independently verify that exclusivity,
  NO-GO; do not claim same-UID race immunity.

### 7. Bounded export, provenance, conflict and application

1. On completion/cancellation/timeout, stop only the exact owned container.
   Verify exact process/container termination and reap/cleanup before exposing
   its rootfs to the exporter. If unresolved, retain ownership and descriptors,
   quarantine, and do not export a potentially mutating tree.
2. Open the task rootfs and `/workspace` through retained descriptors. Enumerate
   with strict no-follow traversal, detecting rename/replacement while reading.
   Reject symlinks, hardlinks, special files, mount crossings, unsafe modes,
   sparse-file excess, duplicate/colliding paths, unknown files outside the
   workspace, and every byte/file/inode/path/output/deadline limit violation.
3. Compare the exact output tree to the input manifest. Emit bounded changed
   file entries, including additions, deletions, preimage digest/absence,
   postimage digest/absence, size, mode policy, and content digest. No
   container-supplied identity, ownership, path, generation, or provenance is
   trusted. Hash bytes while reading the pinned file; stage via exclusive
   descriptor-relative creation and revalidate before publication.
4. Bind the artifact envelope to task/project IDs, immutable snapshot ID/digest,
   source generation, plan/policy/approval provenance, operation ID, image
   digest, resource profile, lease generation/fencing identity, exact result
   manifest digest, and trusted exporter version. Persist the complete artifact
   and digest in the existing registry/result path before task cleanup. Reject
   a stale lease or superseded task even if the artifact bytes are valid.
5. Compare against the latest project generation before any later application.
   If the generation changed, the lease is stale, or any target's current
   preimage differs from the approved baseline, classify the artifact as
   stale/conflicting; never overwrite or automatically rebase it.

**Application boundary:** This runtime-workspace authorization does not
authorize client patch application (Phase 13G remains separate). The safe
application design for a future explicit authorization is: user approval binds
the exact artifact digest, paths, preimages/postimages, and destination project
generation; the Client Agent reopens the approved project root by descriptor,
checks generation and every preimage immediately before mutation, builds a
complete successor tree on the same qualified filesystem, validates and
fsyncs it, then performs a single atomic generation-root/pointer switch under
the project's writer lock. Preserve the original generation for rollback and
record commit outcome in existing durable task/change evidence. If the platform
or current workspace layout cannot atomically switch a whole generation,
do not describe a sequence of per-file renames as an atomic patch: either add a
durable rollback journal with explicit partial-application recovery in Phase
13G, or refuse application. That work requires a separate design/review and
authorization.

### 8. Crash recovery, quarantine, cleanup, and ledger integrity

Use the existing task ledger as the state machine; no competing SQLite database
or filesystem marker becomes authoritative. Proposed monotonic states:

`ALLOCATED → IMPORTING → IMPORT_VERIFIED → CONTAINER_CREATED →
ROOTFS_VERIFIED → READY_TO_START → RUNNING → STOPPING → STOPPED →
EXPORTING → ARTIFACT_COMMITTED → CLEANUP_PENDING → REMOVED`

Any incomplete, contradictory, expired, or uncertain transition enters
`QUARANTINED`; never turn uncertainty into success. Each durable transition
binds current fencing generation, operation ID, exact container ID, task
directory/rootfs identities, project quota ID, snapshot digest and artifact
digest as applicable.

- Partial imports/exports remain private, bounded, and never published. On
  restart, reconcile them against the task record and exact owned descriptors;
  remove only an unambiguous task-owned partial tree after recording the
  disposition. Otherwise quarantine.
- A restart must reauthenticate the current lease/generation and reacquire
  filesystem/runtime observations. Podman labels, path existence, PID alone,
  or missing process are not proof of identity or termination.
- Retain task storage, process/container ownership records, and descriptors
  while execution or cleanup is unresolved. The existing supervisor must
  establish exact process instance, exit/reap, and container absence before
  deleting task storage.
- Persist artifact and integrity/seal evidence before removing container
  storage. If artifact persistence or verification fails, preserve the prior
  ledger and quarantine; do not reseal incomplete state.
- Cleanup is exact-ID and exact-allocation only. Never use runtime-wide prune,
  broad recursive deletion, untrusted paths, or labels as sole ownership.
- Snapshot retention/cleanup remains owned by Core's snapshot store and follows
  its existing references/retention contract; Broker cleanup cannot delete
  Core snapshots or project/client files.

### 9. SQLite VFS same-UID remediation (separate from workspace design)

Do not connect the VFS prototype to the production registry until both the
replacement race and SHM truncation risk are corrected or the threat boundary is
changed and independently verified.

Minimum acceptable remediation path:

1. **Credential separation is mandatory.** Run the registry/VFS in one
   dedicated, non-login UID that no task, Core component, or other untrusted
   process can share. Keep the registry directory and every ancestor private
   and operator-owned. A same-UID attacker cannot be contained by file modes;
   if one remains in scope, stop and redesign around a separate privileged
   brokered storage process/IPC authority. Do not claim a dirfd alone solves it.
2. **Use one exclusive registry owner.** Serialize open/create/rename/delete,
   WAL/SHM mapping, checkpoint, close, and recovery in the owning process.
   Preserve retained DB/journal/WAL/SHM descriptors and OFD locks; validate
   descriptor and directory-entry identity and link counts at every boundary.
   No other SQLite VFS or process may access the database files.
3. **Remove unsafe unlink/reopen assumptions.** Design journal/sidecar lifecycle
   so deletion is permitted only while the VFS exclusively owns the protected
   directory and no peer can replace a name; use private same-filesystem
   staging and atomic rename for creation/publication where supported. Linux
   `unlinkat` cannot condition deletion on the inode previously checked, so do
   not claim this code-level pattern is safe against a concurrent same-UID
   mutator. If such a mutator must be in scope, isolate the VFS in a dedicated
   process/UID and expose only narrow registry operations over authenticated,
   bounded IPC.
4. **Prevent mapped-SHM external truncation.** Keep SHM in the same exclusively
   controlled directory/UID domain, retain and verify its descriptor/mapping,
   serialize resizing and mapping lifecycle, and ensure no untrusted peer can
   open/truncate it. A pre/post size check does not prevent `SIGBUS` from a
   racing truncation. Do not advertise mitigation without enforced custody.
5. **Add adversarial tests and independent review.** Exercise replacement at
   each open/read/write/lock/unlink/close/checkpoint boundary, same-UID and
   distinct-UID peers, descriptor loss, SHM truncation, process crash, partial
   sync failure, and restart reconciliation. Test the actual UID/permission
   isolation in a disposable fixture. Require independent C memory-safety,
   FFI, concurrency, and authority review before a Python adapter is attempted.
   Then verify full CPython registry transaction/cursor/row/exception/rollback
   compatibility against the existing schema and single durable ledger. Keep
   production activation disabled until isolated-VM durability and quota
   acceptance also pass.

**SQLite NO-GO:** A residual hostile same-UID process can replace names or
truncate mapped SHM; no path-based unlink API can make the check and deletion
atomic. Either exclude such a process through an independently enforced UID
boundary or use a separate narrow storage process. Until the independent review
proves the chosen boundary, keep the prototype disconnected.

## GO/NO-GO gates and evidence

### Required deterministic offline tests

The future Gate B implementation must include permanent, unprivileged fixture
tests for at least:

- Exact claim binding, expired/renewed lease, stale source generation,
  signature/audience/task/project/snapshot/digest mismatch, replayed operation,
  and transfer interruption at every chunk boundary.
- Minimum/maximum/over-limit file, byte, inode, directory, depth, path length,
  chunk, sparse allocation, output, and deadline cases; exact-boundary values
  must be tested.
- Traversal, absolute/case-fold/NFC collision, file-as-parent, symlink,
  hardlink, special-file, mount-crossing, unsafe-mode, and source replacement
  attempts during both import and export.
- Descriptor substitution, directory-entry replacement, short reads/writes,
  digest mismatch, duplicate/missing/unexpected manifest entries, and failed
  fsync/rename/publication. Assert no partially verified tree becomes visible.
- Cross-task tree, descriptor, fencing-token, project-ID, registry-record, and
  container-ID substitution. Assert one task cannot read or mutate another
  task's staged data, artifacts, or cleanup target.
- Fake storage-observer results for wrong filesystem UUID/mount ID/project ID,
  missing inheritance, quota disabled, near/exceeded byte or inode limits,
  unexpected writable mount/path, overlay upper/work mismatch, and shared
  writable graph/run roots. All must refuse start.
- A fake Podman adapter for create/copy/mount/inspect/start/stop/unmount
  ordering, wrong returned root path, changed rootfs/mount identity between
  verify and start, duplicate container IDs, unexpected mounts, copy errors,
  timeout, cancellation, output overflow, and ambiguous command completion.
  These tests validate contracts only; they are not Podman acceptance.
- Crash injection after every durable task-state transition, during import,
  rootfs verification, start, stop/reap, export, artifact persistence, and
  cleanup. Reconcile only from the existing ledger and exact identities;
  unresolved identity must remain quarantined with ownership retained.
- Export of added/changed/deleted files, exact preimage/postimage digests,
  stale generation, fencing loss, overlapping changes, missing approval,
  mismatched approved artifact digest, and interrupted artifact commit.
- Application design model: all preimages and generation match, stale
  generation refusal, failure before generation switch leaves the old
  generation current, successful atomic switch publishes the complete new
  generation, and recovery never reports a partial multi-file patch as atomic.
- UID-map and permission fixtures proving the simulated untrusted task identity
  is distinct from Broker/Core identities, plus explicit NO-GO when a task maps
  to the Broker UID. Do not represent an unprivileged fixture as proof of
  production UID allocation or kernel confinement.

### Independent review requirements

Before Gate B, an independent reviewer who did not implement the boundary must
review (1) snapshot authorization and lease/generation binding, (2) descriptor
acquisition and file-tree import/export, (3) Podman copy/rootfs verification
ordering and mount continuity, (4) project-quota/domain inventory and identity
mapping, (5) crash/cleanup reconciliation against the sole ledger, and (6)
artifact approval/application fencing. Native/FFI or quota-helper changes need
a separate focused memory-safety/authority review. A review opinion cannot
promote live acceptance.

| Gate | Prerequisite | GO evidence | NO-GO condition |
|---|---|---|---|
| A. Offline interface review | This approved design | Pinned Podman/OCI docs/source establish copy-in to non-running rootfs, rootfs exposure, pre-start verifier ordering, mount/rootfs continuity through start, and exact supported per-task storage-root configuration. Complete writable-path inventory and threat model independently reviewed. | Any relevant lifecycle/mount/config behavior inferred only from labels, path names, `inspect`, or undocumented CLI behavior; a missing source/mount/quota guarantee. |
| B. Offline contracts/tests | Gate A | Import/export state and bounds implemented behind existing ledger and refusal; deterministic adversarial tests and second-party review pass. No runtime invocation. | Cross-task authority, same-UID task mapping, incomplete limits, silent cleanup/fallback, ledger fork, or test that claims kernel evidence. |
| C. Isolated-VM storage/runtime acceptance | Separate explicit approval and exact VM manifest | Operator-qualified filesystem/UUID/mount/project quotas and inheritance; measured byte/inode enforcement; observed complete writable-domain inventory; exact image/rootless identity; pre-start rootfs identity continuity; isolation/resource/network checks; crash/quarantine/cleanup evidence; independent review. | Any writable path unaccounted/unbounded, project quota or identity not enforced, task and Broker UID overlap, mount continuity unproven, or unresolved work treated as complete. |
| D. Readiness change | All prior gates and separate authorization | Independent acceptance of all applicable 13-row matrix requirements and explicit readiness approval. | Any outstanding matrix blocker, production deployment request without its own authorization, or automatic promotion from offline tests. |
| E. Client patch application | Separate Phase 13G design/review/authorization | Exact artifact approval, generation/preimage CAS, atomic generation switch or durable partial-apply recovery, client-side identity and rollback tests. | This Phase 13F-A.5 approval alone; stale/conflicting generation; no atomic/recoverable application model. |

The current authorized endpoint is **Gate A design only**. No implementation,
Podman command, container, mount, namespace, quota syscall, VM, production
registry adapter, readiness change, or client patch application has been
authorized by this document.
