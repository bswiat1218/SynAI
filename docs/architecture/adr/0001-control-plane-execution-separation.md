# ADR 0001: Separate control plane from execution

- **Status:** Accepted as a mandatory architecture constraint; Broker design
  and implementation are pending. The historical approval record is absent.
- **Date:** 2026-10-10 baseline documentation.

## Decision

SynAI Core is an orchestration control plane and does not receive general
host-execution authority. It has no unrestricted Docker/Podman socket and
does not mount arbitrary host/client project directories. Distributed project
execution, if approved, occurs in a separately identified and restricted
Sandbox Broker. The Broker controls only its own authorized task containers.
The legacy TUI keeps its pre-existing local execution backends under separate
security semantics.

## Rationale

The repository already separates web application APIs from the local TUI
`Tools` and execution backends. Core handles chat, identity, metadata and
snapshot ingestion; browser chat has tool execution disabled. Keeping runtime
control out of Core prevents a web/session compromise from becoming general
host or container administration.

## Alternatives considered

- Give Core direct Docker/Podman access: rejected by the trust invariant.
- Reuse the legacy TUI `Sandbox` or `HostExecution` as a server runner:
  rejected; they are local TUI backends with different image, mount, network
  and host-execution assumptions.
- Execute on Client Agent devices: rejected; device pairing and browser
  authentication do not grant local execution authority.

## Consequences

- Broker is a separate trust domain with a narrow authenticated API and own
  runtime identity.
- Broker outage or invalid response must fail closed; no host fallback.
- Existing Core and TUI remain distinct. This does not disable existing
  explicitly configured local TUI host tools.
- Production service identities, transport, deployment and runtime policy
  need additional design review.

## Implementation dependencies

No Broker exists. Before implementation: define service identity and transport,
durable task claim/fencing authority, rootless Podman deployment, pinned
images, mount/resource/network policy, ownership verification, cancellation
and crash recovery. See [Phase 13F-A acceptance criteria](../synai-2.0.md#phase-13f-a-prerequisites).

## Historical input

The original approved Phase 13A plan and subsequent realignment plan were not
available in the repository. This record reflects the explicit trust
invariants and checked-in Phase 13B/13C runner-boundary notes; it does not
claim to reproduce a missing approval.
