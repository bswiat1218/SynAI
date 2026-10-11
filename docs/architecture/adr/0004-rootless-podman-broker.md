# ADR 0004: Rootless Podman Sandbox Broker

- **Status:** Accepted for Phase 13F-A architecture/design only. Runtime
  implementation and production security acceptance are not approved by this
  decision.
- **Decision date:** 2026-10-10.
- **Approval evidence:** Operator authorization in the Phase 13F-A
  Architecture Sign-Off request dated 2026-10-10. Scope is the ten initial
  Sandbox Broker design choices recorded below; this is not implementation
  approval, runtime isolation evidence, resource-enforcement evidence,
  fencing-test acceptance, or production security acceptance.

## Decision

The Phase 13F-A Broker is a dedicated unprivileged Linux service
identity, separate from Core, TUI and client identities. Rootless Podman is
managed only by this identity, using private runtime configuration, storage
and socket permissions. SynAI Core receives neither Podman/Docker sockets nor
runtime credentials.

Core and Broker communicate over a narrow mutually authenticated API using
mTLS, preferably over a private service network. The raw Podman/Docker API,
socket and runtime credentials are never exposed to Core. The Broker creates
and manages only disposable task containers it allocated and can
independently verify as owned. Each operation is bound to an authenticated
task claim and immutable task-scoped snapshot inputs. The container receives
only a restricted writable task workspace; arbitrary host and client
directories are never mounted.

The Broker accepts only operator-approved images identified by pinned
digests. Networking is disabled by default. The Broker independently enforces
CPU, memory, PID, storage, runtime and output limits, along with fixed
operation, mount, namespace and privilege restrictions. There is no
privileged execution and no silent fallback to Core or host execution.

This is the approved initial architecture for Phase 13F-A design. It is not
an operational Broker or evidence that a particular runtime configuration
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

- Core's durable task-claim ledger is authoritative for claim issuance,
  revocation, fencing generations and recovery. Claims have short,
  implementation-enforced lifetimes; exact maximum TTL and renewal parameters
  must be fixed and tested before deployment.
- The Broker authenticates claims, rejects replay and independently checks
  current Core authority before consequential operations. If current
  authority cannot be verified, it fails closed; expired claims and Core
  disconnection cannot authorize new execution.
- Snapshot retrieval is limited to the exact task-authorized immutable
  snapshot, with independent manifest and content digest verification.
- Allocation ownership, operation states, cancellation/recovery evidence and
  bounded validated result artifacts are durable Broker responsibilities.
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

## Implementation details and acceptance evidence still required

The architecture choices above are approved; these implementation details
must be specified without weakening those choices:

1. Exact mTLS certificate issuance, rotation, revocation and service identity
   lifecycle.
2. Maximum claim/grant TTL, renewal behavior, durable transaction boundaries,
   fencing checks at consequential operations and recovery evidence format.
3. Snapshot transfer size/count limits, grant encoding, staging layout and
   cleanup/retention parameters.
4. Allowed operation kinds, approved image digest update/provenance process,
   concrete resource ceilings and any separately reviewed network exception.
5. Writable staging/export layout and artifact validation/retention details.
6. Ubuntu verification plan, adversarial test cases, deployment monitoring
   and operational recovery procedures.

These are implementation specification and test obligations, not reversals
of the approved architecture. Do not claim isolation, enforcement, fencing
or recovery guarantees until target-runtime tests demonstrate them.

## Implementation dependencies

The approved design is captured by the
[Core-to-Broker contract](../protocols.md#core-to-broker-claim-and-fencing-contract),
[snapshot retrieval contract](../protocols.md#broker-snapshot-retrieval-contract),
and [Phase 13F gates](../synai-2.0.md#17-phase-13f-a-entry-and-13f-b-acceptance-gates).
During 13F-A, implement and test the Broker, claim/fencing, retrieval,
ownership, execution, artifact and recovery deliverables. Do not treat this
design approval as implementation or security-test acceptance.

## Historical input

The supplied [Phase 13 architecture realignment](../history/Phase13ArchitectureRealignment.md)
names rootless Podman, a dedicated unprivileged Broker identity, private
runtime ownership, digest-pinned images, no network by default, bounded task
containers and no Core runtime socket. The historical plan is archived for
traceability but is not the approval evidence for this ADR; the current
operator sign-off above supplies the architecture/design approval.
