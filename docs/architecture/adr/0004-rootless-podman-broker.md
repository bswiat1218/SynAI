# ADR 0004: Rootless Podman Sandbox Broker

- **Status:** Proposed direction; implementation details and deployment
  require design review. Not evidenced as approved by the missing historical
  plan; not implemented.
- **Date:** 2026-10-10 baseline documentation.

## Decision

Distributed task execution is intended to use a separate, dedicated,
unprivileged Sandbox Broker using rootless Podman. Core does not receive the
runtime socket. Broker may administer only task containers it created and
can verify as owned. This ADR establishes a constrained direction, not an
approved wire protocol or production implementation.

## Rationale

Separation limits the consequences of Core compromise and distinguishes
future distributed execution from the existing local TUI Sandbox and
HostExecution backends.

## Alternatives considered

- Core uses an unrestricted runtime socket: rejected.
- Reuse legacy `synai.sandbox.Sandbox`: rejected without a redesign; current
  local behavior has selectable image/runtime and a read-write local
  workspace bind, with bridge network on created containers.
- Use host execution: rejected for the Broker.
- Run workloads on client devices: rejected by independent local authority
  boundary.

## Consequences

- Dedicated UID/service account, rootless runtime ownership, fixed image
  policy, independently enforced quotas and task-scoped containers are
  required.
- No arbitrary host project mounts, privileged mode, host namespaces,
  unreviewed network, devices or fallback.
- Runtime ownership and recovery must be verified before every control
  action.
- Exact deployment, transport, claim source, image verification, limits,
  mounts, result export and crash behavior remain unsettled.

## Implementation dependencies

Phase 13F-A acceptance criteria in the canonical architecture and trust
boundary matrix. There is no Broker source module, Podman implementation or
production deployment manifest at this baseline.

## Historical input

Phase 13B proposes a separate restricted runner; Phase 13C describes a future
Broker with its own identity and policy. Neither checked-in document proves
that rootless Podman specifically was approved. The original approval plan
was unavailable, so status is intentionally proposed.
