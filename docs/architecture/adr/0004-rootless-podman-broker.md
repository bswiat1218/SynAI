# ADR 0004: Rootless Podman Sandbox Broker

- **Status:** Proposed; pending explicit design approval. Not Accepted and
  not implemented.
- **Date:** 2026-10-10 baseline documentation.

## Decision

The proposed Phase 13F-A Broker is a dedicated unprivileged Linux service
identity, separate from Core, TUI and client identities. Rootless Podman is
managed only by this identity, using private runtime configuration, storage
and socket permissions. SynAI Core receives neither Podman/Docker sockets nor
runtime credentials.

Core and Broker communicate over a narrow authenticated API. The Broker
creates and manages only disposable task containers it allocated and can
independently verify as owned. Each operation is bound to an authenticated
task claim and immutable task-scoped snapshot inputs. The container receives
only a restricted writable task workspace; arbitrary host and client
directories are never mounted.

The Broker accepts only operator-approved images identified by pinned
digests. Networking is disabled by default. The Broker independently enforces
CPU, memory, PID, storage, runtime and output limits, along with fixed
operation, mount, namespace and privilege restrictions. There is no
privileged execution and no silent fallback to Core or host execution.

This is a proposed initial architecture, not an approved protocol, an
operational Broker, or evidence that a particular runtime configuration
enforces these restrictions.

## Rationale

Separating orchestration from execution limits the consequences of Core
compromise and prevents browser authentication or project metadata from
acquiring device-local or host execution authority. Rootless Podman provides
a candidate container boundary owned by a distinct service identity. The
Broker remains responsible for independently enforcing its fixed execution
policy even when Core issues an otherwise valid claim.

## Alternatives considered

- Core uses an unrestricted runtime socket: rejected; violates control-plane
  separation.
- Reuse legacy `synai.sandbox.Sandbox`: rejected; current behavior permits a
  selected image/runtime, bridge networking and a read-write local workspace
  bind, and runs under the local TUI's authority.
- Use host execution: rejected for distributed tasks; `HostExecution` is
  explicitly unsandboxed.
- Run workloads on client devices: rejected; local execution requires
  independent Client Agent authorization and is a separate future design.

## Consequences

- Claims, current lease generations, replay handling, snapshot grants,
  ownership records and recovery state require durable, reviewed contracts.
- A claim signature alone is insufficient: the Broker must check current
  authority and its fixed local restrictions before side effects.
- Image selection, operation kinds, resources, mounts, networking and output
  limits are Broker-controlled, not caller-selected.
- Runtime objects must be verified as Broker-owned before inspect, exec,
  collect, cancellation or removal.
- Ambiguous operations become uncertain and must not be automatically
  replayed. A new owner cannot proceed until prior writers are proven stopped.
- Result success requires validated artifacts and provenance; a process exit
  or success message alone does not prove filesystem changes.

## Ubuntu Server 26.04 / rootless Podman assumptions to verify

Implementation and security tests must run on the actual supported Ubuntu
Server 26.04 environment and verify, rather than assume:

- dedicated service-account setup, subordinate UID/GID ranges and isolation
  from other Podman users;
- cgroup v2 availability, delegation and effective CPU, memory and PID
  controls for rootless containers;
- private rootless socket/configuration/storage permissions and behavior
  across service and Podman restarts;
- storage quotas, writable-layer bounds, cleanup and resistance to exhaustion;
- default-disabled networking and any approved exception isolation;
- effective seccomp, AppArmor/LSM, capability, namespace and mount
  restrictions under the deployed kernel/runtime;
- container identity/ownership metadata reliability and resistance to
  malicious or colliding labels;
- termination, cancellation, orphan discovery and crash recovery behavior.

If an essential restriction cannot be demonstrated, deployment must fail
closed; configuration presence alone is not evidence of enforcement.

## Remaining decisions requiring approval

1. Approve this dedicated rootless Podman direction and its service boundary.
2. Approve the Core/Broker authentication mechanism and identity lifecycle.
3. Approve the claim issuer, durable lease-generation authority, fencing,
   expiry, revocation and uncertain-operation recovery model.
4. Approve snapshot-scoped retrieval grants, expiry and maximum transfer
   limits.
5. Approve allowed operation kinds, immutable image digest update/provenance
   process, resource ceilings and any network exceptions.
6. Approve writable staging/export layout, artifact validation and retention.
7. Approve the Ubuntu verification plan, adversarial tests and deployment
   monitoring/recovery evidence.

## Implementation dependencies

Before implementation, review this ADR and the proposed
[Core-to-Broker contract](../protocols.md#proposed-core-to-broker-claim-and-fencing-contract),
[snapshot retrieval contract](../protocols.md#proposed-broker-snapshot-retrieval),
and [Phase 13F gates](../synai-2.0.md#phase-13f-a-entry-and-13f-b-acceptance-gates).
During 13F-A, implement and test the Broker, claim/fencing, retrieval,
ownership, execution, artifact and recovery deliverables. Do not treat design
approval as implementation/security-test acceptance.

## Historical input

The supplied [Phase 13 architecture realignment](../history/Phase13ArchitectureRealignment.md)
names rootless Podman, a dedicated unprivileged Broker identity, private
runtime ownership, digest-pinned images, no network by default, bounded task
containers and no Core runtime socket. The historical plan is archived for
traceability, but no separate formal sign-off was supplied. It therefore
supports the rationale and traceability of this proposal but does not make
this ADR Accepted.
