# ADR 0003: Transfer snapshots through Core's immutable store

- **Status:** Accepted and implemented for bounded source snapshots; Broker
  retrieval is pending. Historical approval record is absent.
- **Date:** 2026-10-10 baseline documentation.

## Decision

Client Agents transfer bounded individual files through authenticated Core
device HTTP endpoints. Core validates their paths, types, limits and content
digests, creates the canonical manifest and stores verified objects in a
private immutable snapshot namespace. The Core snapshot adapter provides
read-only verified source to indexing/context code. No direct Client-to-Broker
transfer or live workspace mount is used.

## Rationale

This preserves immutable source identity, allows Core-side bounded validation
and retention, and prevents the Broker from requiring access to a live client
filesystem. Content, manifest, source device and binding provenance can be
checked independently.

## Alternatives considered

- Broker mounts or reads a client directory: rejected.
- Browser uploads arbitrary paths/archive: rejected.
- Core forwards client bytes directly to an executor: rejected.
- Trust client-declared hashes/manifest as final: rejected; Core recomputes.

## Consequences

- Core receives and retains source content as an authorized application
  responsibility; it must be treated as sensitive untrusted input.
- Snapshots are bounded, expiring and explicit; expired content has no silent
  fallback.
- Exclusions are heuristic, not comprehensive secret scanning.
- Future Broker reads must retrieve the exact snapshot and verify manifest
  and object digests before execution.

## Implementation dependencies

Implemented in `synai/web/snapshots.py`, `distributed.py` and the Phase 13E
client upload flow. Tests: `tests/test_web_distributed.py`,
`tests/test_client_agent.py`. Broker retrieval authorization and an
integrity-bound Core/Broker protocol remain unimplemented.

## Historical input

The checked-in Phase 13C/13E documents provide the available design input.
The original approved realignment plan was not in the repository.
