# Phase 13F-A.5 acceptance inventory

This is the authoritative A.5 acceptance matrix. Offline code, test models,
independently acquired native evidence and production acceptance are different
states. No row can be promoted by a mock, policy value, successful parser, quota
query alone, requested termination or review opinion.

Baseline: `1e929aa0aedef6520f0d56d89f120593b7b7bdae`, clean before this program.
Architecture and existing corrections are documented in
[Phase 13F-A](../phase-13f-a-sandbox-broker.md). No live quota, runtime, service,
privilege, VM or storage operation is authorized by this document.

## Matrix

| Requirement | Existing implementation reused | Offline status / remaining work | Independently required live evidence | Production blocker |
|---|---|---|---|---|
| Native quota / ABI | [ABI](../../prototypes/quota-inspector-abi/README.md), sealed native read-only adapter | OFFLINE_IMPLEMENTED: borrowed-FD, command/output-kind and raw-kernel/libc-result contracts; tests below | Real separately approved GETQUOTA/GETINFO backend, supported descriptor/ID/masks/errors | Native backend unavailable |
| Trusted inventory | Bounded CandidateInventory, complete-policy digest, sealed attestation | OFFLINE_IMPLEMENTED: current-request sealed attestation freshness; MODEL_ONLY source identity/digest/time check | Protected authenticated source and independently approved identity; hash is not trust | Acquisition unavailable |
| Descriptor/mount/namespace/UUID | Native owned descriptors/pre-post checks; Broker Directory/openat2 | Existing validation reused; MODEL_ONLY source replacement and same-namespace mounted-FD checks | Qualified FD/namespace correspondence and protected ancestry | Identity acquisition/handoff unaccepted |
| Existing-file charging | Native listed-file checks; Broker bounded recursive per-file verification | Existing tests reused; acquisition/traversal contract below; no redundant native enumerator | Actual project attributes/charging of all existing files | Kernel charging not accepted |
| Configuration / write denial | Checked counters, explicit configuration state, exact caps | Decoder reused; acceptance-test campaign specified below, not executed | Independent active enforcement and separately approved destructive sparse/concurrent byte/inode exhaustion | No empirical enforcement evidence |
| SQLite VFS | Single registry ledger, DB/sidecar/seal/marker pins and lifecycle checks | MODEL_ONLY: file-kind/operation-bound before/after FD contract; existing checkpoint/crash/absence tests reused | Actual identity-preserving native VFS, lock/fsync/crash behavior | external_filesystem_resolution_unaccepted; sqlite_file_quota_vfs_unaccepted |
| Runtime bind source | Descriptor/allocation checks and unconditional refusal | MODEL_ONLY: mounted-source inode/filesystem/same-namespace correspondence; cross-namespace unavailable | Mounted-source inode/filesystem/namespace correspondence, not source-path text | bind_source_resolution_unaccepted |
| Podman storage | Strict TOML/digests/environment/report/layer/path policy | Existing parsers/rejections reused; new all-lifecycle-launch-refusal regression | Version-qualified graph/run/overlay/layer/scratch/tmpfs/log/config observer | runtime_storage_acceptance_pending |
| Aggregate storage | Unique domain accounting, remaining entitlement, watchdog reserves | Existing exact-threshold/domain/remaining-entitlement tests reused unchanged | Continuous observed capacity under complete workload and metadata pressure | Trusted live capacity/enforcement missing |
| Exact containment | Exact record/ID/image/labels; inspect/stop/kill/inspect/absence; single-flight | Existing event-order/failed persistence/single-flight/recovery tests reused unchanged | Independently verified exact-ID stop/absence and preserved state on failure | Runtime unavailable; unresolved is not stopped |
| Supervisor / crash recovery | Native retained leases/terminal latch; Broker owned threads/children/reconciliation | MODEL_ONLY: test-only exact PID/start/generation/boot/namespace exit/reap/cleanup lease model wired into actual Inspection tests | Exact process identity, physical completion/reaping/cleanup/restart | Adapter completion promise not yet qualified |
| Privilege/confinement | Broker zero-effective-capability/identity checks, restricted arguments | MODEL_ONLY: untrusted effective-capability/identity/groups/no-new-privileges/seccomp/LSM requirements and negative checks | Dedicated identities; reviewed helper permissions, seccomp/LSM and deployed effective state | Helper/deployment confinement unaccepted |
| Broker authorization/readiness | Claims/leases/fencing/replay, registry/seals/quarantine/watchdog | Existing F1-F5/R1-R6/C1-1 reused; no production Broker code or gate changes | All preceding gates plus independent security acceptance | All existing refusals preserved |

Each live requirement is `INTEGRATION_PENDING` and, when provisioning, privileges,
quota writes or runtime execution are needed, `PRIVILEGED_APPROVAL_REQUIRED`.
Tests of candidate validators/lifecycle models are `MODEL_ONLY`, even when passing.
An implementation is `OFFLINE_VERIFIED` only for its explicitly tested semantics.

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
