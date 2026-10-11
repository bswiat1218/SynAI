# Phase 13F-A — Isolated Sandbox Broker

Phase 13F-A adds a separately launchable `synai-broker` process and its
standalone package under `synai/broker/`. It does **not** connect the Broker
to browser tasks, model-generated tool calls, `CodingAgentRuntime`, or the
legacy retired interface. No Core code receives Podman authority. The Core task records
remain disabled until the follow-on integration has been reviewed.

## Reused architecture

The Broker is not a second coding-agent engine. Phase 13F-B is expected to
adapt the existing `ExecutionBackend` contract and reuse `CodingAgentRuntime`,
`Tools.call`, Phase 10 policy and per-operation approvals, plan validation,
verification, repair/review bounds, `TaskChangeTracker`,
`CheckpointManager`, and model-routing provenance. None of those paths is
changed or invoked by this package. Legacy retired interface sandbox and separately
authorized host behavior remain on their existing paths.

Dependency map: Phase 13C immutable snapshot store → exact task-scoped
retrieval authorization → Broker validation and private immutable staging →
Broker-owned writable task copy → fixed rootless runtime and typed operation
protocol → bounded result evidence. A later Phase 13F-B adapter can connect
the existing `ExecutionBackend`/`CodingAgentRuntime` orchestration while
retaining Core policy, approvals, verification, and checkpoint authority.
The Core retrieval authorization and lease channels are acceptance blockers,
not mocked production integrations.

The source-of-truth flow remains Client Agent → Core immutable snapshot store
→ Broker. The Broker's `SnapshotRetriever` interface is exact-ID/digest/task
scoped and never accepts an archive or a source path. Snapshot validation,
private immutable staging, and creation of a separate writable workspace are
implemented in the Broker package. A production Core retrieval adapter and
the authoritative live-lease validator are not connected in 13F-A. The
Broker refuses preparation without a retriever and refuses every operation
without a live-lease validator. No direct Client Agent → Broker path exists.

## Service boundary and protocol

`synai-broker` serves a version-1 HTTP protocol over a Unix-domain socket.
The listener accepts only the health probe without authentication; protocol
routes require a 30-second timestamp, one-use 128-bit nonce, and
HMAC-SHA-256 over method, exact path, timestamp, nonce, and request-body
digest. Used nonces are persisted in the private Broker SQLite ledger. The
socket is placed in an operator-provisioned setgid directory shared with the
Core service group; the CLI refuses unsafe directory ownership/modes and
does not provision accounts, groups, directories, or host policy.

Every operation has a strict versioned request and an Ed25519 Core-signed
task claim. The Broker is configured with only the Core public key. Claims
bind task, project, snapshot ID and manifest digest, operation ID and
argument digest, deadline, target/profile, operation kind, plan/policy/
approval provenance, lease generation/fencing token, image digest, and fixed
resource profile. The Broker independently verifies signature, expiry,
registered task identity, immutable provenance, arguments, resource/image
binding, replay, and live lease through an injected authoritative validator.
Approval provenance is metadata in a Core-signed claim; it is not accepted as
proof of user consent by itself. The Core-side claim issuer and live lease
validator remain intentionally unwired; there is no task-claim endpoint.

The request body is limited to 2 MiB, claims to 16 KiB, task claims to 15
minutes, individual files to 1 MiB, snapshots to 500 files/64 MiB, paths to
512 characters/16 components, command argument vectors to 64 arguments/4
KiB, and output to the selected fixed profile ceiling. Errors use bounded
structured codes and do not return runtime stderr or exception details.

## Service identity and runtime

The process requires a non-root UID distinct from the configured Core UID.
Its private state directory, SQLite task/operation/replay/ownership ledger,
task workspaces, and Unix socket are owned by the Broker identity. Only this
process invokes the configured Podman executable. Core receives neither a
runtime socket nor a runtime-admin API. A Broker command is never passed to a
host shell: Podman is invoked with an argument vector, and approved container
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
identity or Core-shared socket group was provisioned. The process must not
run under the current Core identity; the code does not create a service
account. Therefore the current machine cannot pass Broker runtime readiness.

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
completed, failed, interrupted, uncertain, cleaning_up, or removed. The
operation ledger claims a unique operation ID before effects. Process restart
marks claimed operations uncertain, marks interrupted tasks uncertain, and
quarantines associated container records. It never automatically retries
commands or performs runtime-wide cleanup. Before task-container inspection,
execution, collection, cancellation, termination, or removal, runtime state
is compared to the private task/allocation/container/image/profile record;
labels and caller IDs alone do not establish ownership. Cleanup is scoped to
one verified record and is idempotent where outcome evidence is conclusive.

Collection derives changed files from the actual staged working copy and
records preimage/postimage SHA-256 values and a checksum of the bounded
change manifest. Core-side independent result validation, durable Phase 9
change tracking/checkpoint provenance, and patch application are not
implemented here. Nothing is applied to a client.

## Enablement and security boundary

The package can be installed with the existing project distribution. To
launch it, an operator must first provision (outside SynAI) the distinct
unprivileged Broker identity, its rootless Podman configuration and sub-UID
mapping, the private state directory, the restricted shared-group socket
directory, service HMAC key, Core Ed25519 public key, Core UID, and one
reviewed digest-pinned local image. Configure the corresponding
`SYNAI_BROKER_*` environment variables and run `synai-broker` as that
identity. Missing configuration is a startup error; missing acceptance
capabilities produce unavailable readiness, not a host or privileged
fallback. No image download or container is started by installation or
startup.

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
absence of secrets from user-approved source. Shared socket-group
configuration and key storage need deployment-specific least-privilege
review.

Phase 13F-A is **not accepted for execution**. In addition to runtime
capabilities and a reviewed image, acceptance still requires a real Core
snapshot-grant/retrieval endpoint, Core-signed claim issuance, a live
authoritative lease/fencing validation channel, Core-side result validation,
bounded artifact/diff persistence, restart reconciliation against actual
Podman state, cancellation/container-loss integration tests, cross-project
runtime isolation, and independent security review. The Broker API is not
wired to Core or exposed to model/browser task execution while these items
remain open.
