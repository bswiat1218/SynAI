# Phase 13F-A.5 acceptance inventory

This is the authoritative A.5 acceptance matrix. Offline code, test models,
independently acquired native evidence and production acceptance are different
states. No row can be promoted by a mock, policy value, successful parser, quota
query alone, requested termination or review opinion.

Baseline: `1e929aa0aedef6520f0d56d89f120593b7b7bdae`, clean before this program.
Architecture and existing corrections are documented in
[Phase 13F-A](../phase-13f-a-sandbox-broker.md). This document does not authorize
live quota, runtime, service, privilege, VM or storage operations.

Current integration baseline: `91aed9174347ced528afb1e8088ac02e571f6a15`
("Phase 13F-A.5 Autopilot"), clean worktree/index at campaign start.
Category 1 (integration code A1-A6) is authorized; VM preparation and all live
kernel/runtime/destructive operations remain separately gated.

Category 1 continuation baseline: `fe6842caeb745fce54ef8dcb4dd6088e8ca2faee`
("Phase 13F-A.5: Real Integration and Acceptance Campaign"), clean worktree and
index. The earlier eight-file transport change set is already incorporated in
this history; it was not present as unstaged work at continuation start.

Category 1 offline-implementation baseline: `cbe4cfb2c9578466edb5ccbb061d32146a38029d`
("Phase 13F-A.5 Category 1 Continuation: Unblock Real Integration"), clean
worktree and index before this task.

Category 1 A1 feasibility status (read-only inspection, no Podman invocation):
installed Podman 5.7.0 documentation describes `--mount` `src`/`source` as a
bind mount source spec and `--preserve-fd`/`--preserve-fds` as descriptors passed
to the container process (the numbered-FD form is documented for `crun`). These
are not an interface for the runtime to consume an already-open FD as mount
source authority. No installed source or authoritative interface establishes
that handoff or independent destination-object verification before workload
start. The runtime branch remains blocked and fail-closed.

Workspace decision status: the user approved Option B as the target for offline
implementation. Bounded descriptor-backed snapshot import, provenance-bearing
staging, bounded artifact export, and a fake-only ordered lifecycle model are
implemented and tested below. No Podman runtime adapter or production
execution authorization was added. The installed Podman 5.7.0 `cp` manual
describes running or stopped containers but does not establish support for
created-but-never-started containers; its `mount` manual returns a path but
does not establish pre-start-to-start identity continuity or quota attribution.
No Podman command or container was invoked. Exact copy/rootfs lifecycle,
storage-root isolation, mount continuity, and quota attribution remain
NO-GO/acceptance requirements. See the
[implementation-ready workspace design](phase-13f-a5-workspace-architecture-decision.md).

## Matrix

| Requirement | Existing implementation reused | Offline status / remaining work | Independently required live evidence | Production blocker |
|---|---|---|---|---|
| Native quota / ABI | [ABI](../../prototypes/quota-inspector-abi/README.md), sealed native read-only adapter | CATEGORY1_CODE: isolated callable typed x86_64 `quotactl_fd` wrapper; deterministic dispatch tests and non-invoking link test; no production wiring | Separately approved GETQUOTA/GETINFO against qualified descriptors/kernel and independently trusted evidence | Native backend unavailable |
| Trusted inventory | Bounded CandidateInventory, complete-policy digest, sealed attestation | Implemented fixed-path `/etc/synai/quota-inspector/inventory.json` loader using safe `openat2` descriptor-relative access; requires root-owned, non-writable ancestry, fixed owner/group/mode, single-link bounded regular file, before/after/reopen identity match, bounded parse, per-call freshness. Returns only candidate plus pinned source FD; disposable-root test seam is cfg(test) only | Operator-protected installation, actual path/ancestry ownership, deployment source lifecycle; separately acquired filesystem UUID/mount/namespace and project evidence | Native inspection and trusted attestation remain unavailable |
| Descriptor/mount/namespace/UUID | Native owned descriptors/pre/post checks; Broker Directory/openat2 | Broker path-resolution checks exist; source loader pins only the fixed policy file. It does not acquire storage-root descriptors or assert filesystem UUID/mount/namespace/project identity | Qualified storage FD/namespace correspondence, project identity, replacement/remount detection | Native filesystem identity acquisition unavailable |
| Existing-file charging | Native listed-file checks; Broker bounded recursive per-file verification | Existing tests reused; acquisition/traversal contract below; no redundant native enumerator | Actual project attributes/charging of all existing files | Kernel charging not accepted |
| Configuration / write denial | Checked counters, explicit configuration state, exact caps | Decoder reused; acceptance-test campaign specified below, not executed | Independent active enforcement and separately approved destructive sparse/concurrent byte/inode exhaustion | No empirical enforcement evidence |
| SQLite VFS | Single registry ledger, DB/sidecar/seal/marker pins and lifecycle checks | OFFLINE_PROTOTYPE: same-engine CPython registration; descriptor-relative DB/journal/WAL/SHM, OFD locks, multiple connections/processes, transactions, checkpoints, replacement refusal and process-crash recovery pass 19 tests. Independent review confirms unresolved check/use and same-UID mutation race risks, including mapped-SHM truncation/SIGBUS. No Python registry adapter or production activation | Full registry callback/transaction compatibility, independently protected custody, live filesystem identity/quota charging, power-loss durability and broader failure injection | Prototype is custody-dependent only; production-grade hostile-mutation containment and registry compatibility remain unaccepted |
| Runtime workspace source and rootfs identity | Descriptor/allocation checks and unconditional refusal | OFFLINE_VERIFIED for tested importer/exporter semantics: exact snapshot/project/task/snapshot-digest/lease-generation provenance; bounded descriptor-relative import into separate immutable and writable trees; private publication only after verification; partial imports remain unpublished; bounded provenance-bearing changed-file artifacts; collection requires durable completion, exact recorded container ownership and verified termination. Fake lifecycle model permits only ordered evidence and never authorizes execution. A distinct live project-generation claim is not supplied by the current snapshot contract. Podman 5.7.0 manuals leave created-state `cp` and pre-start rootfs continuity unproven. See [workspace design](phase-13f-a5-workspace-architecture-decision.md) | Authenticated source snapshot and generation freshness at real authority boundary; created-but-never-started copy semantics; independently observed container-rootfs identity, upper/work and all writable-domain quota attribution; identity continuity through start; real termination and bounded export | Runtime launch refusal remains; fake adapter evidence is never sufficient; any identity/storage uncertainty is NO-GO |
| Podman storage | Strict TOML/digests/environment/report/layer/path policy | Existing parsers/rejections reused; offline design requires per-task graph/run roots or equivalently proved isolation, project quota ID/inheritance, and accounting for every writable path. No runtime invocation or storage reconfiguration authorized | Version-qualified graph/run/overlay/layer/scratch/tmpfs/log/config observer; actual filesystem UUID/mount/project-ID/inheritance and byte/inode quota enforcement | runtime_storage_acceptance_pending; shared/unattributed writable storage is NO-GO |
| Aggregate storage | Unique domain accounting, remaining entitlement, watchdog reserves | Existing exact-threshold/domain/remaining-entitlement tests reused unchanged | Continuous observed capacity under complete workload and metadata pressure | Trusted live capacity/enforcement missing |
| Exact containment | Exact record/ID/image/labels; inspect/stop/kill/inspect/absence; single-flight | Existing event-order/failed persistence/single-flight/recovery tests reused unchanged | Independently verified exact-ID stop/absence and preserved state on failure | Runtime unavailable; unresolved is not stopped |
| Supervisor / crash recovery | Native retained leases/terminal latch; Broker owned threads/children/reconciliation | Broker child cleanup refuses handles outside its owned-child set; ordinary child kill/reap, foreign handle and same-PID/different-instance refusal, unknown child after supervisor-instance restart, late spawn and interrupted cleanup are exercised. Native `WorkerLease` remains a test-only model; no restart-durable ownership exists | Independent supervisor, exact process identity across restart/reboot, physical completion/reaping/cleanup and retained ownership across Broker failure; ordinary child reaping does not prove uninterruptible-work completion | Adapter completion promise and crash-surviving ownership not qualified |
| Privilege/confinement | Broker zero-effective-capability/identity checks, restricted arguments | MODEL_ONLY: untrusted effective-capability/identity/groups/no-new-privileges/seccomp/LSM requirements and negative checks | Dedicated identities; reviewed helper permissions, seccomp/LSM and deployed effective state | Helper/deployment confinement unaccepted |
| Broker authorization/readiness | Claims/leases/fencing/replay, registry/seals/quarantine/watchdog | Existing F1-F5/R1-R6/C1-1 reused; no production Broker code or gate changes | All preceding gates plus independent security acceptance | All existing refusals preserved |

Each live requirement is `INTEGRATION_PENDING` and, when provisioning, privileges,
quota writes or runtime execution are needed, `PRIVILEGED_APPROVAL_REQUIRED`.
Tests of candidate validators/lifecycle models are `MODEL_ONLY`, even when passing.
An implementation is `OFFLINE_VERIFIED` only for its explicitly tested semantics.

## Category 1 implementation record

Category 1 was the only live authorization used in this campaign. The following
implementation and validations occurred; they are not real kernel/runtime
acceptance:

- Added a minimal libc `syscall()` transport in the separate ABI crate for only
  `GETQUOTA` and `GETINFO`, with fixed syscall number 443, unsigned arguments,
  fixed GETINFO ID zero, initialized 8-byte-aligned output and immediate errno
  capture. Unsafe declarations/calls are confined to the ABI transport module;
  the native validator still forbids unsafe and does not depend on or call it.
- Added deterministic internal dispatcher tests for exact fd/command/id/size,
  raw status, malformed size and invalid decoded evidence. Unit-test calls to the
  public real entrypoint panic before dispatch. The integration link smoke test
  takes the function address only; it does not call it. No test or command issued
  a quota syscall.
- Focused independent ABI boundary review found no significant remaining issue.
  Its initial documentation finding was corrected. No independent kernel/privilege
  acceptance was performed.
- Mount handoff feasibility checked from installed Podman 5.7.0 documentation and
  package state only. The docs describe pathname bind sources and FD passthrough
  into the workload; `crun` is absent. This does not establish descriptor-bound
  mount source resolution, so the runtime branch remains blocked, fail-closed.
- Initial SQLite investigation: CPython 3.12.11 reports SQLite 3.46.1; `_sqlite3` has ELF NEEDED
  `libsqlite3.so.0`. The resolved system library is 3.46.1-9ubuntu0.3, SHA-256
  `4633a4be13fdf05cb0f859e04fb06077a1fe30a90e405721a23873bd1f3d88a8`; current
  `_sqlite3` extension SHA-256 is
  `8a8ac76ba4d4a8652ccd316ec77fb778756d9f45fea6ea8fb8b88239d0e8f76a`.
  The installed header exposes 18 `sqlite3_io_methods` callbacks and 16 VFS
  callbacks. Python registry source depends on Connection execute/executescript,
  row factory, transaction state/context, commit/rollback/close; Cursor iteration,
  fetchone/fetchall, rowcount; `sqlite3.Row` name/index behavior; and sqlite3
  exception subclasses. Existing durability uses WAL, synchronous=FULL,
  `wal_checkpoint(TRUNCATE)`, sidecar identity checks, sealing and a dirty marker.
  At that investigation point, no VFS implementation demonstrated the complete
  callback set or WAL/SHM semantics. The later isolated prototype below now
  demonstrates selected DB/WAL/SHM operations, but not the full registry contract
  or durable behavior. Therefore the Python registry adapter was **not changed**.
- An attempted standalone direct-child owner prototype was removed after review
  identified abandonment and stale-identity weaknesses. The existing tested
  sticky terminal latch and test-only ownership model remain; no unsafe or
  inadequately owned process supervisor was retained.
- Newly executed ABI checks: 25 unit tests, 1 installed-header C integration,
  1 non-invoking transport link integration, 15 compile-fail doctests; locked
  offline debug/release builds, formatting, warnings-denied Clippy all pass.
- Newly executed native checks after removal of the rejected prototype: 51 unit
  tests, 1 non-test unavailable integration, 8 compile-fail doctests; locked
  offline debug/release builds, formatting and warnings-denied Clippy pass.
- The existing Python/Broker suites were not rerun because no Python registry,
  Broker/runtime source or behavior was changed. Historical suite results above
  remain historical.

Category 1 has not achieved full Stage A: deployment-qualified trusted inventory,
complete filesystem/project identity acquisition, physical supervisor ownership
across Broker/supervisor death, production-grade VFS authority, and runtime
storage/mount evidence remain blocked. A compiled low-level syscall transport
alone grants no observation authority. All 13 acceptance rows and production
refusals remain intact.

## Category 1 continuation results

The absolute `$HOME/.cargo/bin/cargo` and `rustc` tools were present and used.
No toolchain or dependency was installed. Native test fixtures run only in
disposable temporary roots; the production inventory source path remains fixed.

### Native SQLite prototype

Added [the isolated dirfd VFS prototype](../../prototypes/sqlite-dirfd-vfs/).
Loading its extension through CPython `sqlite3`, registering the named VFS
through the extension API table, then opening a separate CPython connection with
that VFS proves registration and SQL execute through the same engine; this is
stronger than matching version strings. The prototype uses descriptor-relative
database and rollback-journal open/create, initialized short reads, bounded
pread/pwrite-style I/O, truncation, fsync, directory sync after creation,
Linux OFD byte-range locks, and fail-closed path/file checks. Tests cover
rollback/commit/reopen, multiple and cross-process connection lock exclusion,
hot-journal process-crash recovery, truncation, directory replacement,
symlink/hardlink/special-file rejection, URI/path rejection, unsupported
extension/multi-database operations and descriptor cleanup. Process-crash
recovery is not power-loss durability proof.

The current isolated prototype supports tested WAL/SHM operations, OFD locking,
checkpoints, DB/journal/WAL/SHM replacement detection and cross-process crash
recovery. Its registered methods and SQL operations use the same SQLite engine,
established by loading the extension through CPython and then opening separate
CPython connections with the registered VFS—not by matching version strings.
The Python registry adapter, schema and durable ledger remain unchanged.

This does **not** establish race-free containment against malicious same-UID
mutation. Name checks and descriptor-relative `openat`/`unlinkat` are not atomic;
a same-UID process can replace entries between checks or directly truncate a
mapped SHM file (including causing `SIGBUS`). The prototype is custody-dependent
test evidence, not production-grade authority. Readonly WAL, disk temporary
files, multi-database super-journals, full Python registry compatibility,
power-loss durability and exhaustive failure injection remain unqualified.
See the prototype README for exact boundaries.

### Protected inventory acquisition

Added a fixed-path native loader using Rustix's safe `openat2` API and owned
descriptors. It rejects symlinked ancestry/source, validates root-owned
non-writable ancestry and exact configured root/file ownership and modes, checks
regular-file/single-link/size bounds, reads via the pinned FD, compares
before/after and path-reopened identity, checks request freshness, and parses
the immutable policy candidate. A descriptor remains retained in the result.
Only the unit-test build accepts a disposable root. `NativeInspector` now has a
configured-source entry point but still routes through the existing sealed
validator; the non-test backend remains unavailable, so the loader cannot
produce a `TrustedInventory`, query quotas, or publish success. The source
loader authenticates the inventory file only; it does not establish storage
filesystem identity, namespace/mount correspondence or project attributes.

### Physical supervision

The Broker refuses cleanup requests for a process handle outside its current
exact owned-child set. New real unprivileged tests verify that a timeout kills
and reaps an owned child and that a newly constructed supervisor refuses an
unknown child while the original owner retains it. A same-PID/different-process
handle test also verifies identity-safe refusal. Existing adversarial tests cover
late spawn, timeout, cancellation, unresolved cleanup and retained ownership.
This remains ordinary child-process supervision only: memory is not
restart-durable, and no test proves completion of uninterruptible kernel work.

### Workspace decision and verification

The single recommendation is **B, isolated task staging**, recorded in
[the workspace architecture decision](phase-13f-a5-workspace-architecture-decision.md).
The user approved it for offline design only, not implementation or execution.
The approach preserves immutable snapshot provenance and avoids exposing a
mutable client-project mount, but requires an independently verified per-task
quota-controlled writable container rootfs/upper and complete Podman storage
accounting. If that cannot be qualified, the design returns for a new decision;
no weaker mount strategy is implied.

Newly executed final checks: native debug tests (56 unit, 1
production-unavailable integration, 8 compile-fail doctests), release build and
release production-boundary test, formatting and warnings-denied Clippy passed.
The final WAL/SHM-capable SQLite VFS suite passed 19 tests. Focused Python
process-deadline tests passed 20 tests; full Python discovery passed 751 tests
(4 skipped). Independent review found no significant inventory-loader issue; the
SQLite review confirmed the documented same-UID namespace/SHM race limitation,
which remains an explicit production blocker rather than being represented as
solved. Final `py_compile` and diff checks passed.

## Remaining separately approved categories

Only Category 1 has been authorized. The following boundaries remain distinct:

| Category | Exact scope still requiring explicit approval |
|---|---|
| 2 - VM preparation | Operator-selected hypervisor and exact VM UUID; pinned guest image/kernel/toolchain/snapshot; 4-vCPU/8-GiB-RAM planning default; bounded 16-GiB OS disk; exact newly attached 32-GiB scratch-device serial; destructive format/mount/quota/project/inheritance setup; guest-only identities, subordinate IDs, cgroup/seccomp/LSM/service configuration; pinned artifacts/media and no implicit downloads; independent host/hypervisor supervision; exact cleanup/revert and evidence manifest. No lab/host target. |
| 3 - Read-only kernel qualification | Exact prepared VM/boot/device/inventory manifest; only fixed descriptor-bound GETINFO(id=0), GETQUOTA(registered project IDs), and enumerated read-only identity/configuration reads; helper UID/capability allowlist, deadlines and outputs; no quota writes, filesystem formatting/mount setup, exhaustion or container execution. |
| 4 - Destructive enforcement | Exact C-qualified scratch project domains; bounded byte/inode/concurrent/existing-file writers, explicit quota/configuration mutations, drift/failure scenarios, independent evidence and VM snapshot restore. Scratch-only destruction; no host/OS/lab files. |
| 5 - Real runtime/VFS acceptance | Exact approved VM, kernel, one rootless runtime and pinned image; SQLite/VFS transactions, WAL/SHM/checkpoint/crash, source-identity handoff, all writable runtime domains, exact task-owned lifecycle/containment and bounded failure tests. Requires successful prerequisites and new approval; no runtime-wide cleanup, network dependency installs, model commands or 13F-B. |
| 6 - Production deployment | Future exact host, package/configuration/enablement operations, privileges, backup/rollback, external supervision, independent full-row A.5 review and separate enablement decision. No production target or command is currently selected or authorized. |

Category 2 cannot be approved against unresolved placeholders. Its manifest must
name the exact VM UUID, snapshot and guest boot, source-image digest, scratch
block serial, package/artifact digests, mount and filesystem parameters,
identities/capabilities, configuration changes, resource/output/deadline caps,
independent supervisor and recovery destination. The prepared filesystem must
demonstrate at least 15.75 GiB and 241,664 free inodes while preserving the
7.5-GiB entitlement ceiling, 8-GiB continuous reserve and 256-MiB allowance;
128 GiB remains the absolute future allocation ceiling. Nominal disk size is not
usable-capacity evidence. Preparation approval does not authorize Categories 3-6.

## Executed baseline (not final evidence)

- A.5.1c.2a Rust: ABI20 unit +1 header +13 compile-fail; native43 unit +1
  unavailable +5 compile-fail; fixture21; protocol0 standalone; release unavailable
  and header tests passed. All four crates built debug/release offline, fmt and
  Clippy with warnings denied passed.
- Python v1 compatibility52 passed in28.857s during c.2a.
- Coordinated-program baseline Broker255 passed in110.179s.
- Existing Starlette/httpx deprecation and asyncio slow-task diagnostics are not
  acceptance evidence or newly corrected issues. Final program results follow
  after implementation; earlier results must not be recounted as final results.

## Final newly executed offline evidence

| Check | Result |
|---|---|
| ABI units / compile-only C integration / compile-fail | 22 /1 /15 passed |
| Native units / unavailable integration / compile-fail | 56 /1 /8 passed |
| Fixture Rust | 21 passed; binary/docs0 tests |
| Shared protocol | 0 standalone; unchanged goldens covered by Python compatibility |
| Release native unavailable / ABI header | 1 /1 passed |
| All four crate debug/release builds, fmt, Clippy -Dwarnings | Passed, locked/offline; no Rust warnings |
| Focused external-storage/lifetime/safety Python suites | 115 passed in49.746s |
| SQLite descriptor-relative VFS compatibility suite | 19 passed; includes WAL/SHM, locking, checkpoints and process-crash recovery |
| Focused Broker process-deadline suite | 20 passed |
| Final full Python discovery (includes Broker and v1 compatibility) | 751 run:747 passed,4 skipped,173.682s |
| Python compilation | Modified Broker/test sources compiled |
| Source/manifests/features/dependency closure and release symbols | No real quota invoker/activation or native fixture dependency |
| Whitespace / source diagnostics | Passed; index unchanged |

Commands used existing installed tools and dependencies:

```sh
cargo test --locked --offline --manifest-path prototypes/<crate>/Cargo.toml
cargo build --locked --offline --manifest-path prototypes/<crate>/Cargo.toml
cargo build --locked --offline --release --manifest-path prototypes/<crate>/Cargo.toml
cargo fmt --manifest-path prototypes/<crate>/Cargo.toml --check
cargo clippy --locked --offline --all-targets --manifest-path prototypes/<crate>/Cargo.toml -- -D warnings
python -m unittest tests.test_broker_runtime_storage tests.test_broker_existing_file_quota tests.test_broker_authority_lifetimes tests.test_broker_safety_transitions
python -m unittest discover -s tests
git diff --check
```

`<crate>` means ABI, native, protocol and fixture packages, not a new workspace.
Release integration tests additionally used `cargo test --release --test
production_boundary` for native and `--test header_layout` for ABI, locked/offline.
No package/toolchain installation occurred. No actual quota query or Podman
command was executed. Optional container and Ollama integration tests remain
skipped, not acceptance passes.

Python emitted one existing Starlette/httpx deprecation and179 asyncio slow-task
diagnostics during full discovery. No ResourceWarning, RuntimeWarning, destroyed
task or unretrieved-exception messages. Expected injected error diagnostics in
negative tests are not unexplained runtime failures.

Focused independent review found no significant inventory-loader issue. The
SQLite review confirmed that descriptor-relative operations plus sequential
identity checks do not prevent malicious same-UID name replacement or SHM
truncation races. These remain explicit limitations; no review opinion substitutes
for the executed tests or missing live evidence.

New permanent tests cover raw/libc return conventions, malformed/failed output
and command-buffer mismatches, borrowed-FD lifetime restrictions, acquisition
binding/size/freshness/source replacement, namespace correspondence, all four VFS
file kinds across nine operations and replaced identities, confinement negatives,
sealed attestation freshness, exact-worker identity/exit/reap/cleanup, retained
leases after dropped requests/crash, sticky terminal outcomes and every runtime
lifecycle command retaining the launch refusal. Existing F1-F5/R1-R6/C1-1,
capacity thresholds, file inventory and containment tests were reused, not replaced.

All matrix rows with implemented offline validation are now OFFLINE_VERIFIED for
their stated semantics. Rows explicitly MODEL_ONLY remain models. Every live
acceptance row remains INTEGRATION_PENDING / PRODUCTION_BLOCKED.

### Option B bounded staging continuation

Offline implementation in `synai/broker/snapshots.py` consumes only the exact
snapshot returned by the existing authenticated Core retrieval interface. It
binds task, project, immutable snapshot ID/digest, lease generation and import
operation; validates the manifest; applies file/byte/path/depth/deadline/inode
and allocated-block limits; uses descriptor-relative creation and verification;
and publishes the workspace last. A failed or partial import remains
unpublished and quarantined. The immutable source copy and writable task copy
are separate. Current claims do not include a separate live project-generation
value; source freshness is bounded by the immutable snapshot identity and lease
generation, not asserted against mutable project state.

Collection in `synai/broker/app.py` requires completed durable task state,
recorded exact container ownership and verified termination. It rejects unsafe
tree entries and emits bounded changed-file content with preimage/postimage
digests, provenance and a manifest envelope; artifacts are persisted under the
task slot and are never applied to the original project. The result's
`artifact_manifest_sha256` is the digest of canonical changed-file entries for
Core compatibility; `envelope_sha256` separately binds the stored envelope.
The exact final result envelope is size-checked before artifact persistence.

The offline lifecycle model exercises fixed ordered fake-evidence transitions
and assurance loss only. It permanently reports production execution as
unauthorized; no production adapter consumes its fake evidence.

The focused independent security review found and prompted correction of two
collection defects before final verification: the changed-file digest had been
computed over the wrapper rather than the Core-validated change list, and the
result duplicated the complete artifact payload. The full artifact is now
persisted once; the Core result carries the bounded change list once. Review
also confirmed that incomplete staging and uncertain cleanup must retain
quarantine; failed snapshot retrieval and interrupted copy tests assert that
state.

New verification for this continuation: 117 Broker, filesystem and Core result
authority tests passed; full Python discovery ran 761 tests (757 passed, 4
skipped); the final focused staging/import/quarantine set ran 33 tests and
passed; modified Python files compiled with `py_compile`; `git diff --check`
passed. No repository Python formatter/linter is configured and `ruff` is not
installed. Python emitted the existing Starlette/httpx deprecation and asyncio
slow-task diagnostics. No Rust source changed, so no Rust suite was rerun in
this continuation; the installed absolute-path Cargo toolchain is available.
Podman 5.7.0 manual pages were inspected only. No Podman command, container,
quota syscall, mount, VM, privileged setup, production registry activation or
project application was performed.

The exact Podman `cp` behavior for a created-but-never-started container,
rootfs/overlay writable-domain accounting, and independent identity continuity
through start remain NO-GO. Fake adapters and offline tests do not satisfy those
acceptance gates. SQLite VFS same-UID replacement and SHM-truncation risks also
remain unresolved and the prototype remains disconnected from the registry.

## Storage invariants

Hard entitlements7.5GiB /221,184 inodes; continuous reserve8GiB /16,384 inodes;
operational allowance256MiB /4,096 inodes; initial available capacity15.75GiB /
241,664 inodes. Count each quota domain once and use remaining entitlement, not
hard caps added to free capacity. Candidate sizes24/32/64/128GiB; maximum128GiB.
Nothing here authorizes allocation, formatting, mounting or quota modification.

## Completion classification

- Offline implementation: authorized code, bounded contracts/models/tests/docs
  completed; real adapters/VFS/runtime and kernel evidence explicitly pending.
- Isolated-VM acceptance: separately approved live experiments and review.
- Fully accepted A.5: every required acceptance gate and independent review passes.

Only the first classification is reachable by this offline program. Production
Broker execution and Phase13F-B remain unavailable/out of scope.

## Acquisition and external-boundary specifications

The new [native candidates](../../prototypes/quota-inspector-native/src/contracts.rs)
and [ABI acquisition types](../../prototypes/quota-inspector-abi/src/acquisition.rs)
are deliberately untrusted. No matching values or successful Result authenticate
a source or construct native authority. Expected identities must eventually be
independently acquired; metadata supplied by the same caller is only a model.
No transport, VFS, mount handoff, process supervisor or confinement policy was
installed. The worker model is test-only and non-test native inspection unavailable.

Complete role acquisition must enumerate bounded existing files, not merely the
native four listed leaves. Reuse the existing Broker recursive inode/depth bound
and per-file FD verifier. Account DB/WAL/SHM/journal/seals/markers/config/logs,
runtime metadata and task artifacts; reject omitted/unknown entries, traversal
cycles, unsafe permissions/links/project domains, mounts and exhausted bounds.
Sequential pre/post observations do not establish an atomic filesystem snapshot.
Dedicated identity and protected namespace ancestry must prevent untrusted races.

Future VFS integration must preserve independently pinned identities at every
open/create/read/write/truncate/lock/sync/checkpoint/close boundary and bind both
file kind and operation. Verify a new sidecar before its first write; do not
reinterpret a vanished active sidecar as safe absence. Confirm checkpoint/close
and cleanup before retiring pins. Crash, failed fsync/quota, lock inconsistency,
replacement or interrupted sealing remain unresolved; retain the existing sole
registry ledger and historical evidence, never create a replacement or reseal
uncertain success. This document and candidate checks do not implement SQLite
locking/fsync semantics or qualify an actual VFS.

The future exact-worker supervisor must independently acquire PID/start ticks,
boot/PID-namespace and generation binding, preserve owned descriptor leases through
physical execution, and report Complete only after exact-instance completion/
reaping and safe cleanup. Signals and caller cancellation are not those facts.
No fresh request or restart may release unresolved leases, infer absent work from
missing history or accept a recycled PID. The fake model's observations are not
kernel completion proof. A real supervisor and durable recovery remain separate.

## Consolidated future isolated-VM approval package — NOT AUTHORIZED

### Prerequisites and target identity

Target VM identity, image digest, kernel/filesystem/runtime versions and storage
device are **UNSELECTED**. No host or existing quota-lab path is a default target.
The proposed campaign is non-executable until those exact identities, affected
paths/resources, backup/disposal procedure and operator approvals are recorded.
Nominal candidate storage is24/32/64/128GiB, never above128GiB; actual formatted
usable capacity must satisfy all totals/reserves. No allocation is authorized.

A separately approved real read-only quota backend (with independently reviewed
minimal unsafe invocation if necessary), trusted inventory loader, exact-worker
supervisor, native VFS and runtime identity/storage observer must exist before
their corresponding experiments. Current offline contracts are not substitutes.
Do not remove production gates to perform tests; use the explicitly approved
isolated qualification harness and retain fail-closed Broker behavior.

### Campaign R — read-only qualification

Proposed operations: qualify approved filesystem FD kind/flags and immutable
inventory source; read exact project GETQUOTA/GETINFO only; inspect mount/namespace/
UUID/device/project correspondence and existing-file assignments; qualify masks,
units, errno, unsupported kernels and missing records. Record all inputs/output
hashes, target versions and descriptor/source identities independently.
Do not execute legacy queries, quota enumeration/mutation or device-path fallbacks.
No successful query alone passes enforcement.

Required privilege is **UNQUALIFIED** until the approved target-specific kernel/
filesystem permission checks establish the minimum. CAP_SYS_ADMIN is broad and
not a quota-only grant; no automatic grant is acceptable. Broker remains zero
effective capabilities, separate from any approved restricted helper identity.
Reviewed syscall allowlist, no-new-privileges, LSM/capability bounds, protected
inventory/socket and exact-process supervision must be specified before launch.
This package does not install seccomp/AppArmor/systemd policy.

Read-only syscall intention does not eliminate setup/mount/runtime side effects.
Campaign R does not authorize storage provisioning or Podman initialization.
Those operations need explicit inclusion in the independently approved environment
setup; captured reports are not permission to start runtime commands.

### Campaign D — separately approved destructive enforcement/runtime tests

Only on the exact disposable approved filesystem and owned test domains:
byte/inode hard exhaustion, sparse allocation, concurrent writers, existing-file
charging, WAL/SHM/log/metadata exhaustion, reserve pressure, overlay/image scratch/
tmpfs accounting, configuration/source replacement, assurance loss, exact-owned
container stop/kill/absence, blocked-worker cancellation/crash/reaping and recovery.
Quota enablement/configuration, formatting/mounting, image loading or container
execution must be listed as explicit setup/operations, not implied by Campaign R.

Data may be modified or destroyed in these domains. Never target the host workspace,
Core state/secrets or existing quota lab. A trusted external campaign supervisor
must bind every worker/container to exact recorded identity, impose bounded
time/output/queue budgets, confirm termination/reaping and preserve evidence.
Unknown identity or unresolved physical work means stop admission, quarantine,
retain ownership/evidence and operator recovery—not broad kill/delete or slot reuse.

### Stop/recovery, evidence and NO-GO

Stop for wrong/unknown VM/device/namespace/image, missing capabilities/confinement,
unsupported interface/masks, stale/inconsistent evidence, unexpectedly writable
authority ancestry, capacity/reserve failure, quota bypass, identity drift,
unconfirmed containment/reaping or failed durable evidence writes. Do not retry
privileged fallbacks or change policy ceilings to turn a failure into success.

Recovery must inspect exact owned processes/containers/files and historical registry,
confirm physical cleanup and preserve interrupted state before separate authority
can be re-established. If exact ownership cannot be proven, use operator-assisted
recovery; do not enumerate arbitrary runtime objects or manufacture fresh history.

Acceptance evidence includes pinned target/version/config/LSM/syscall details,
immutable inventory/acquisition bindings, exact FD/process/container identity,
empirical write-denial and reserve measurements, syscall/error/mask observations,
VFS callback/locking/fsync/crash traces, runtime effective storage/mounted-source
evidence, cleanup/reconciliation results and independent review. Any required
missing row is NO-GO. Actual-host deployment, Broker enablement and Phase13F-B
remain separate later approvals even if the isolated campaign succeeds.
