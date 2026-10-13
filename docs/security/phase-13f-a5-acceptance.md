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

Category 1 A1 feasibility status (read-only inspection, no Podman invocation):
installed Podman 5.7.0 documentation describes `--mount` `src`/`source` as a
bind mount source spec and `--preserve-fd`/`--preserve-fds` as descriptors passed
to the container process (the numbered-FD form is documented for `crun`). These
are not an interface for the runtime to consume an already-open FD as mount
source authority. No installed source or authoritative interface establishes
that handoff or independent destination-object verification before workload
start. The runtime branch remains blocked and fail-closed.

## Matrix

| Requirement | Existing implementation reused | Offline status / remaining work | Independently required live evidence | Production blocker |
|---|---|---|---|---|
| Native quota / ABI | [ABI](../../prototypes/quota-inspector-abi/README.md), sealed native read-only adapter | CATEGORY1_CODE: isolated callable typed x86_64 `quotactl_fd` wrapper; deterministic dispatch tests and non-invoking link test; no production wiring | Separately approved GETQUOTA/GETINFO against qualified descriptors/kernel and independently trusted evidence | Native backend unavailable |
| Trusted inventory | Bounded CandidateInventory, complete-policy digest, sealed attestation | Candidate validation and freshness are implemented; the authenticated operator-source selector/loader and native descriptor acquisition are missing, not merely awaiting kernel evidence | Protected authenticated source, independently approved identity, and separately acquired filesystem/project evidence; hash is not trust | Native inspection unavailable; source acquisition contract absent |
| Descriptor/mount/namespace/UUID | Native owned descriptors/pre/post checks; Broker Directory/openat2 | Broker path-resolution checks exist; no native acquisition backend binds the protected root, ancestry, filesystem UUID/mount and namespace or independently queries project attributes | Qualified FD/namespace correspondence, protected ancestry, project identity and replacement/remount detection | Native identity acquisition unavailable |
| Existing-file charging | Native listed-file checks; Broker bounded recursive per-file verification | Existing tests reused; acquisition/traversal contract below; no redundant native enumerator | Actual project attributes/charging of all existing files | Kernel charging not accepted |
| Configuration / write denial | Checked counters, explicit configuration state, exact caps | Decoder reused; acceptance-test campaign specified below, not executed | Independent active enforcement and separately approved destructive sparse/concurrent byte/inode exhaustion | No empirical enforcement evidence |
| SQLite VFS | Single registry ledger, DB/sidecar/seal/marker pins and lifecycle checks | No prototype: current Python `sqlite3` API does not expose VFS registration, and no pinned same-engine native adapter/VFS exists. A new C boundary would have to implement and test the complete VFS callback, WAL/SHM/locking/sync, URI restriction, and Python transaction/cursor/error contract before any adapter change. No production registry changes made | Disposable DB/WAL/SHM/journal concurrency, failure, restart and process-crash tests; separately, real filesystem identity, quota charging and power-loss acceptance | Code blocker: no reviewed same-engine VFS/adapter boundary. Live filesystem/durability acceptance remains separate |
| Runtime bind source | Descriptor/allocation checks and unconditional refusal | Podman 5.7.0 docs show path-valued mount source and workload-only FD passing; no supported descriptor-authoritative mount-source handoff or independent target-object proof was found. No runtime change made | Mounted-source inode/filesystem/namespace correspondence before workload, not source-path text | bind_source_resolution_unaccepted |
| Podman storage | Strict TOML/digests/environment/report/layer/path policy | Existing parsers/rejections reused; new all-lifecycle-launch-refusal regression | Version-qualified graph/run/overlay/layer/scratch/tmpfs/log/config observer | runtime_storage_acceptance_pending |
| Aggregate storage | Unique domain accounting, remaining entitlement, watchdog reserves | Existing exact-threshold/domain/remaining-entitlement tests reused unchanged | Continuous observed capacity under complete workload and metadata pressure | Trusted live capacity/enforcement missing |
| Exact containment | Exact record/ID/image/labels; inspect/stop/kill/inspect/absence; single-flight | Existing event-order/failed persistence/single-flight/recovery tests reused unchanged | Independently verified exact-ID stop/absence and preserved state on failure | Runtime unavailable; unresolved is not stopped |
| Supervisor / crash recovery | Native retained leases/terminal latch; Broker owned threads/children/reconciliation | Broker child cleanup now refuses handles not in its exact owned-child set. Deterministic tests include a real unprivileged child timeout/kill/reap. Native `WorkerLease` remains a test-only model; no separate supervisor or restart-durable ownership exists | Independent supervisor, exact process identity across restart, physical completion/reaping/cleanup and retained ownership across Broker failure; ordinary child reaping does not prove uninterruptible-work completion | Adapter completion promise and crash-surviving ownership not qualified |
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
- SQLite evidence: CPython 3.12.11 reports SQLite 3.46.1; `_sqlite3` has ELF NEEDED
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
  No VFS implementation exists to demonstrate that the complete callback set,
  WAL/SHM locking/mapping, Python API, transaction or crash-durability semantics
  are preserved. Therefore the Python registry adapter was **not changed**.
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

Category 1 has not achieved full Stage A: real trusted inventory acquisition,
complete filesystem/project identity acquisition, physical supervisor across
Broker/supervisor death, VFS implementation, and runtime storage/mount evidence
remain blocked. A compiled low-level syscall transport alone grants no observation
authority. All 13 acceptance rows and production refusals remain intact.

## Category 1 continuation results

This continuation made only the directly safe process-ownership correction:
`ChildProcesses.cleanup` now rejects a foreign `asyncio.subprocess.Process`
handle before signalling or waiting on it and makes the unresolved state sticky.
Its tests prove both that an unowned live process is left running and that a real
unprivileged child spawned by the owner is killed and reaped on timeout. These
results cover ordinary child processes only; they do not establish PID/start-time
acquisition for an external worker, descriptor-lease survival after Broker death,
or completion of uninterruptible kernel work.

Newly executed continuation checks: the focused `ProcessDeadlineTests` suite
passed (18 tests); full Python discovery passed (749 tests, 4 skipped); Python
compilation and `git diff --check` passed. The focused independent review found
no significant issue in the ownership guard or real-child tests. Rust ABI/native/
fixture builds, tests, formatting and Clippy were not rerun because neither
`cargo` nor `rustc` is installed in this checkout environment; no toolchain or
dependency was installed. Previous Rust results remain historical.

The SQLite branch stopped before implementation. The registry uses CPython's
`sqlite3.Connection`/`sqlite3.Row`, implicit connection context transactions,
explicit `BEGIN IMMEDIATE`/commit/rollback, `executescript` schema creation and
migrations, `rowcount`, `fetchone`, cursor iteration/tuple conversion, WAL,
`synchronous=FULL`, `wal_checkpoint(TRUNCATE)`, foreign-key/integrity checks,
and DB/WAL/SHM/journal identity/seal/dirty-marker handling. The public Python
module provides no VFS registration function. A prototype would therefore need
a same-engine native registration/adapter boundary plus a complete VFS: short
I/O/error propagation, file-control and locking semantics, shared-memory
mapping/locking, deletion/access, sync and checkpoint behavior, restricted
temporary/URI/extension/attachment handling, and parity with the registry's
Python API. Implementing only file-open callbacks or routing the existing
registry through a second SQLite engine would violate the requirement. No adapter,
schema, ledger, or production activation was changed. Process-crash tests, if
later added, cannot establish power-loss durability.

Trusted acquisition is not solely a VM blocker. The native crate currently has
untrusted inventory and identity candidates plus a sealed acquisition interface,
but its only non-test backend refuses to attest, acquire a root, or return native
filesystem evidence. No authenticated fixed source/root identity or production
loader is selected. Broker `Directory` path checks do not automatically supply
the native helper with the same authority, and promoting its candidate values
would create a competing trust source. A safe offline implementation first needs
the protected source contract and a native descriptor-relative acquisition
adapter; actual project attributes, quota state, UUID/mount correspondence and
live replacement/remount acceptance remain additional kernel/VM evidence.

The mounted-source feasibility decision remains **NO-GO for the present Podman
interface**: `--preserve-fd` hands descriptors to a process in the container;
`--mount type=bind,src=...` accepts a source specification. Passing an FD to
Podman, or to `crun`, does not show that the runtime uses that FD as mount-source
authority. No independently inspectable post-mount destination identity
mechanism was established. Alternatives requiring a new decision are a reviewed
OCI-runtime/helper extension that consumes an already-pinned descriptor and
proves the mounted object, or avoiding a host project bind and staging bounded
data through a separate runtime-owned channel with independently accepted
storage accounting. Neither alternative is implemented or authorized here.

Rust revalidation was unavailable in this continuation: `cargo` and `rustc` are
not installed in the current environment. No toolchain or dependency was
installed. This does not alter prior Rust results, but those historical results
are not newly executed results for this continuation.

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
| Native units / unavailable integration / compile-fail | 51 /1 /8 passed |
| Fixture Rust | 21 passed; binary/docs0 tests |
| Shared protocol | 0 standalone; unchanged goldens covered by Python compatibility |
| Release native unavailable / ABI header | 1 /1 passed |
| All four crate debug/release builds, fmt, Clippy -Dwarnings | Passed, locked/offline; no Rust warnings |
| Focused external-storage/lifetime/safety Python suites | 115 passed in49.746s |
| Final full Python discovery (includes Broker and v1 compatibility) | 747 run:743 passed,4 skipped,167.184s |
| Python compilation | 71 files passed |
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

Focused independent reviews of acquisition/authority/ownership contracts and the
final VFS operation-binding delta reported no confirmed vulnerabilities. No review
opinion substitutes for the executed tests or missing live evidence.

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
