# ADR 0001: Separate control plane from execution

- **Status:** Accepted as a mandatory architecture constraint. The restricted
  Broker implementation exists; Core integration and runtime security
  acceptance remain incomplete.
- **Date:** 2026-10-10 baseline documentation.

## Decision

SynAI Core is an orchestration control plane and does not receive general
host-execution authority. It has no unrestricted Docker/Podman socket and
does not mount arbitrary host/client project directories. Distributed project
execution occurs in a separately identified and restricted Sandbox Broker.
The Broker controls only its own authorized task containers. The Core web
request path does not dispatch through the standalone local container backend.

## Rationale

The repository separates web application APIs from the retained agent `Tools`
and execution-backend contracts. Core handles chat, identity, metadata and
snapshot ingestion; browser chat has tool execution disabled. Keeping runtime
control out of Core prevents a web/session compromise from becoming general
host or container administration.

## Alternatives considered

- Give Core direct Docker/Podman access: rejected by the trust invariant.
- Reuse the standalone local `Sandbox` as a server runner: rejected; its
  configurable runtime, image, bridge network and workspace mount do not meet
  Broker isolation requirements.
- Execute commands directly in Core: rejected; Core must have no general
  host-execution authority.
- Execute on Client Agent devices: rejected; device pairing and browser
  authentication do not grant local execution authority.

## Consequences

- Broker is a separate trust domain with a narrow authenticated API and own
  runtime identity.
- Broker outage or invalid response must fail closed; no host fallback.
- Existing local agent modules remain separate from the Core web request path;
  their execution semantics are not Broker guarantees.
- Production service identities, transport, deployment and runtime policy
  need additional design review.

## Implementation dependencies

The restricted Broker service, claim-signature checks and rootless Podman
adapter are implemented and tested. Before production use, verify service
identity and transport, Core-authoritative claim/fencing integration, pinned
images, mount/resource/network enforcement, ownership verification,
cancellation and crash recovery in the deployment environment. See [Phase
13F-A acceptance criteria](../synai-2.0.md#17-phase-13f-a-entry-and-13f-b-acceptance-gates).

## Historical input

The original Phase 13A plan copies are not included in the active documentation
tree. This record reflects the explicit trust invariants and checked-in Phase
13B/13C runner-boundary notes; it does not claim to reproduce a missing
historical approval.
