# Offline native inspector foundation (A.5.1c.0)

This is a library-only, unprivileged development foundation. There is no native
executable, listener, service, installed inventory loader, kernel quota call,
runtime integration, or privilege grant. **Broker execution remains unavailable.**
Do not deploy it or count its tests as kernel acceptance.

## Compile-time boundary and protocol

- [Native library](./src/lib.rs) depends only on serde/serde_json, SHA-256 and
  the fixture-free [protocol identity library](../quota-inspector-protocol/src/lib.rs).
  It does not depend on the fixture inspector package.
- The existing [disposable harness](../quota-inspector/src/bin/fixture-harness.rs)
  remains separate. Its corrected R1-R6 parser, replay, rate, startup and cleanup
  implementation is unchanged. Its role type and canonical digest are re-exported
  from the shared library, not independently reimplemented.
- The native fake backend exists only under `#[cfg(test)]`. There are no Cargo
  features, environment variables or public adapter-injection methods to select
  synthetic observations in a non-test native artifact.
- Non-test `NativeInspector` always fails unavailable: trusted acquisition reports
  `inventory_authority_unavailable`; every descriptor/query method also refuses
  with `native_backend_unavailable`. Expected values cannot create a success.
- Rustdoc compile-fail tests prohibit public observation construction, inventory
  trust promotion, fixture imports and backend injection. An integration test
  links the **non-test** native library and repeatedly checks its refusal. Python
  invokes that test executable and parses its actual error envelopes through the
  unchanged v1 client. No native service executable is created.
- Error envelopes have exactly the existing eight v1 fields; successful test
  observations have exactly the existing 26 fields. No protocol fields, status,
  challenge, epoch or generation semantics changed. Parsed request/schema success
  is not quota observation success. `ValidatedObservation` has no public
  constructor or deserializer.

## Inventory is policy until separately authenticated

[CandidateInventory](./src/inventory.rs) parses bounded operator-policy bytes
without opening any path. A matching digest and safe-looking pathname **do not
attest trust**. Acquisition is a sealed adapter contract; its current non-test
implementation always refuses. Test attestation is deliberately synthetic.

The immutable candidate requires all seven production purposes: task slots,
Podman graph/runtime, image scratch, Broker state/logs and temporary storage.
Non-task purposes occur once; task slots may repeat within the role bound.
Role IDs and project IDs must be unique, paths disjoint and beneath a single
approved filesystem root, and shared ancestry must have coherent ownership/mode.
Project IDs are distinct on this one filesystem; cross-filesystem project
correspondence and bind mounts are intentionally unsupported.

The native policy includes the unchanged ten-field v1 role definition plus
directory modes, filesystem device/mount/root/namespace expectations and bounded
persistent-file purposes. Lower-case canonical UUID, ext4, full mount root `/`,
nonzero namespace inode/mount ID, exact UID/GID and 0700/0750 directory modes are
required. A second SHA-256 binds **all original policy bytes**, including modes
and file checks, while v1 generation still hashes only its original ten role
fields with Python-compatible sorted compact JSON. That byte digest is a binding,
not an authentication mechanism.

Native hard byte limits must also be exactly representable in 1024-byte quota
units; unaligned limits are refused rather than rounded. This narrows native
policy acceptance, not Python v1 or existing Broker configuration.
Aggregate hard entitlements cannot exceed 7.5 GiB or 221,184 inodes; adding
approved domains cannot silently increase the existing entitlement budget.

`TrustedInventory` is issued only inside the sealed observation pipeline after
independent acquisition has supplied a matching full-policy binding, v1 generation,
opaque approval identity and approved consumer namespace. A future real loader
must prove operator authorization and source-descriptor/ancestor ownership,
permissions, pre/post file stability and immutable policy acquisition. None is
claimed from the current fake attestation. No privileged inventory is read here.

The sample [test policy](../../tests/fixtures/quota_inspector_native/inventory.json)
contains synthetic paths and metadata, **not observations or trusted authority**.
It is included only in test binaries, never the native library.

## Descriptor-backed pipeline

[Evidence types](./src/evidence.rs) model owned `OwnedFd` handles, namespace-local
filesystem identity, project identity/inheritance, inode/type/link/owner/mode,
quota configuration, counters and sampling times. Exported raw evidence types
are untrusted data and do not grant the ability to publish success.

[Observation validation](./src/observation.rs):

1. Binds the opaque role request to immutable policy/generation; no caller paths,
   device IDs, project IDs or quota commands are accepted.
2. Acquires the separately attested root descriptor.
3. Resolves each single component with read-only, beneath, no-symlink,
   no-magiclink and no-mount-crossing rules.
4. Checks each directory type, exact owner/mode, UUID, device major/minor,
   mount ID/root/type and namespace. The final directory must independently
   have the expected project ID and inheritance.
5. Opens each approved existing file relative to the pinned role descriptor.
   Its actual project ID, filesystem/mount, owner, 0600 mode, regular-file type,
   nonzero inode and single link are checked independently.
6. Validates the attested root, every retained descriptor and each parent/child
   correspondence before and after the quota operation. Replacements, inode
   aliases, closed/unavailable descriptors and mount drift cannot become success.
7. Requires complete coherent quota evidence and checked units, matching project/
   hard limits, bounded usage/capacity and fresh per-operation timestamps.

The adapter must eventually implement **kernel-backed** descriptor resolution and
identity checks. Passing requested resolution flags to a fake is only a contract
test, not proof those restrictions were enforced by a kernel. Root attestation
and parent/child checks are not atomic protection against malicious same-UID
rename/substitution. A later backend must document the actual race and privilege
boundary. Holding a descriptor pins its object, not a globally immutable path.

Mount IDs are compared only inside the approved namespace. Unknown correspondence
is unavailable; an observed different namespace is inconsistent even with an
equal mount ID/device. No pathname-match or cross-namespace-ID shortcut exists.
UUID/device correspondence must later be established independently from trusted
kernel/device evidence rather than copying policy fields.

SQLite DB/WAL/SHM and the persistent log are separate existing-file checks.
Correct directory inheritance never proves charging of old files. This narrow
model requires its listed files to exist; absence is unavailable, not a fabricated
zero-use observation. It does not enumerate all runtime/persistent files or replace
the Broker's existing bounded traversal, file-verifier or native SQLite VFS gate.

## Read-only quota contract and future Linux ABI qualification

The sealed adapter exposes only `read_project_quota` on an already pinned
filesystem descriptor and inventory-bound `ProjectId`, plus nonblocking completion
polling. It has no setters, enable/disable/sync methods, arbitrary ioctl/command
dispatch, block-device paths, shell fallback or alternative quota interface.

`ReadOnlyQuotaFields` models untrusted optional fields and validity bits. It
requires all limit/usage fields, rejects missing/unknown bits, converts hard
block limits with checked **1024-byte** multiplication, treats current space as
bytes and inode limits/usage as counts. This is **not an ABI buffer or syscall
implementation**. Before/after quota evidence must agree; disabled/accounting-only
state is inconsistent and unknown state unavailable. Reported enforcement state
is not empirical write-denial acceptance.

No `quotactl_fd()` declaration, FFI, syscall number dispatch or query is implemented.
Later separately approved isolated-VM work must verify:

- The exact target kernel/libc/header ABI for `quotactl_fd(fd, cmd, id, addr)`,
  supported descriptor kinds/flags, architecture syscall mapping, structure size,
  alignment, initialization and returned validity masks. Local headers describe
  syscall 443 on x86-64 and asm-generic; this is not portable execution evidence.
- `PRJQUOTA = 2` and `QCMD(command, type)` encoding. Candidate read-only questions
  include `Q_GETQUOTA` (`0x800007`), `Q_GETINFO` (`0x800005`) and, only if separately
  reviewed as necessary, `Q_GETFMT` (`0x800004`). No arbitrary selector is permitted.
  The exact accepted command subset must be reviewed before any implementation.
- `if_dqblk` limit/usage validity, `dqb_bhardlimit` in 1024-byte units,
  `dqb_curspace` in bytes, inode counts, zero/unlimited limits, grace/soft-limit
  semantics and overflow. `if_dqinfo` flags and valid bits do **not by themselves**
  establish enforcement rather than accounting-only state.
- Minimum UID/capability requirements, user/mount namespace effects, ext4 project
  quota support, unsupported/permission errors and reliable read-only evidence for
  active enforcement. `CAP_SYS_ADMIN` is broad authority, not quota-only permission.
- Project attributes on actual files/directories, descriptor-mounted filesystem
  UUID/device correspondence and checked `fstatvfs` block/inode translations.

If the reviewed interface cannot establish required evidence on a kernel, that
kernel stays **unavailable/NO-GO**. Never substitute another privileged quota API.
Actual bounded byte/inode exhaustion and denial tests require **separate** approval.

## Resource and lifetime limits

| Resource | Bound |
|---|---|
| Inventory input | 16,384 bytes, JSON depth 16 |
| Roles | 32, immutable after parsing |
| Relative role path | 8 components; canonical path <= 1,024 bytes |
| Listed existing files | <= 4 per role; required state checks 3, log check 1 |
| Owned observation descriptors | <= 13 (root + components + files); tested state maximum 12 |
| Active operation | 1 per inspector owner; concurrent admission returns `busy` |
| Operation budget | > 0 and <= 500 ms, monotonic; no automatic retry |
| Protocol payload | <= 16,384 bytes; fixed enum error codes, no arbitrary diagnostic buffer |

The controller retains a pending operation and its descriptor lease across
timeout/cancellation/unresolved ownership. It polls for confirmed completion
without admitting another request or turning a timeout into a finished syscall.
Completed work after expiry/cancellation cannot yield success. No worker queue
or native kernel work is started.

Query progress explicitly distinguishes `Pending(reason)` from
`Complete(Result<evidence, error>)`. Even an error requires a confirmed completion
declaration before descriptor release; an adapter timeout/cancellation is not an
implicit completed syscall. Regression tests cover both adapter-reported and
controller-reported cancellation/expiry.

The request's first terminal failure is latched independently of physical work
progress and descriptor ownership. Adapter-reported cancellation/timeout cannot
be cleared by later valid or erroneous completion, repeated blocking, or another
failure. Local cancellation records the already-expired deadline if observed
first; otherwise cancellation remains authoritative across later expiry.
Only `Blocked` and `UnresolvedOwnership` are nonterminal pending reasons; all
other pending failures revoke success for that request. Confirmed completion
finalizes only the old request, not a fresh independently admitted request.
`Complete` requires the backend to have finished using its lease; unresolved
work or cleanup must remain `Pending`. Releasing an owned descriptor is not a
claim of independently supervised process cleanup or kernel enforcement.

## Coordinated offline acquisition and handoff contracts

[Candidate contracts](src/contracts.rs) check untrusted inventory-source
before/after identity, policy/generation binding, size and acquisition time;
same-namespace mounted-source descriptor correspondence; per-kind SQLite
DB/WAL/SHM/journal identity; and required claimed confinement properties.
These checks return no authority token. Expected metadata supplied by a caller
is NOT authenticated acquisition, and matching values cannot create
TrustedInventory or ValidatedObservation. Non-test inspection remains unavailable.

The native package now has a protected-source loader for the fixed
`/etc/synai/quota-inspector/inventory.json` path. It opens each ancestry
component without symlinks, requires root-owned non-writable ancestors and an
exact root-owned `0755` source directory, then reads a single-link `0644`
regular file through a retained FD with a strict size bound and pre/post/reopen
identity comparison. It checks freshness against the request's `Instant` and
parses only after acquisition. `AcquiredInventory` exposes the parsed candidate
and pinned source descriptor; it is **not** a `TrustedInventory` and cannot
attest filesystem UUID/mount/namespace or project attributes. The disposable
root injection exists only in unit-test configuration and is not a production
trust anchor. `NativeInspector::inspect_configured` routes the candidate through
the existing sealed validator, whose production backend still refuses authority.
No real storage-root acquisition, FS ioctl, kernel quota query, VFS registration,
or mounted-source handoff is activated.

The [test-only supervisor contract](src/supervision.rs) models an exact worker
instance using PID, start ticks, generation, boot epoch and PID namespace.
It retains a lease until exit, reap and safe cleanup are all reported for that
same instance. It is compiled only under cfg(test) and wired into a fake backend
to test the actual Inspection state machine; it cannot supervise a real process
or grant production authority. A simulated output being available is insufficient
to finalize pending physical work. An independent simulated worker lease survives
request-owner drop. Actual restart persistence and process-instance acquisition
are separately required, not demonstrated by these models.

Cross-namespace mounted-source evidence stays unavailable; equal mount numbers
are not correspondence proof. VFS candidates model individual operations only:
future native callbacks must establish independently approved descriptors at
every open/create/read/write/truncate/lock/sync/checkpoint/close boundary, including
sidecar lifecycle, absence, crashes and cleanup. Existing Python registry tests
cover interruption, checkpoint, sidecars and sealing; no fake is a native VFS.
SQLite VFS and runtime bind-source readiness refusals remain unconditional.

Complete role acquisition still requires bounded recursive enumeration beyond
the four listed native leaves: reuse the Broker's inode/depth bounds and actual
per-file project checks; refuse truncation, omission, cycles, special files,
cross-mount entries or an unverifiable namespace edge. This is an integration
contract, not a new native enumerator or evidence of complete role charging.
Empirical sparse/concurrent byte/inode write denial needs separate approval.

The authoritative [A.5 matrix](../../docs/security/phase-13f-a5-acceptance.md)
distinguishes verified offline code, test-only models and pending live acceptance.

Deterministic blocked-work tests use an **independently owned simulated worker**
holding an `Arc` descriptor lease; even dropping the request owner does not release
that worker's descriptors until it confirms completion. A later actual worker must
independently own that lease, and its completion polling must itself be nonblocking.
The test does not prove cancellation of real blocked kernel work.

Deadlines cannot interrupt uninterruptible syscalls, SIGSTOP or scheduler stalls.
A later VM deployment requires a separately reviewed process supervisor with exact
child ownership, bounded termination/reap and explicit unresolved recovery.
RAII cannot run after SIGKILL/abort/power loss; this phase implements no supervisor
or system service and offers no atomic same-UID cleanup guarantee.

Storage planning is unchanged: 7.5 GiB hard entitlements, 8 GiB continuous reserve,
256 MiB allowance, 15.75 GiB initial available capacity and 241,664 available inodes.
24/32/64/128 GiB are candidates only, maximum 128 GiB. This library does not qualify
aggregate production capacity, provision or resize anything.

## Offline validation

Use already-installed tooling and cached locked dependencies:

```sh
cargo test --locked --offline --manifest-path prototypes/quota-inspector-native/Cargo.toml
cargo build --locked --offline --manifest-path prototypes/quota-inspector-native/Cargo.toml
cargo fmt --manifest-path prototypes/quota-inspector-native/Cargo.toml -- --check
cargo clippy --locked --offline --manifest-path prototypes/quota-inspector-native/Cargo.toml --all-targets -- -D warnings
cargo test --locked --offline --manifest-path prototypes/quota-inspector/Cargo.toml
cargo build --locked --offline --manifest-path prototypes/quota-inspector/Cargo.toml
python -m unittest tests.test_quota_inspector_prototype
python -m unittest discover -s tests -p 'test_broker*.py'
```

Native fakes use only cloned descriptors of a newly created disposable seed file.
They neither inspect real project quotas nor access the existing quota lab.
The non-test integration executable is a finite test runner, not a daemon.
No fake or fixture can satisfy production readiness. Generic existing inspector
endpoint configuration can technically target a fixture server; v1 has no
cryptographic fixture distinction. Independent production filesystem, quota,
SQLite VFS, bind-source, runtime, containment and post-crash gates remain required.
