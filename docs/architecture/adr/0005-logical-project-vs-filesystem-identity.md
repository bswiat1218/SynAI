# ADR 0005: Logical project identity versus legacy filesystem identity

- **Status:** Accepted compatibility boundary; implemented. Historical
  approval record is absent.
- **Date:** 2026-10-10 baseline documentation.

## Decision

Distributed logical projects use Core-generated opaque IDs independent of any
filesystem path. Device IDs, workspace-binding IDs, snapshot IDs, task IDs,
execution targets and Broker allocations use separate namespaces. Phase 13B
path-based projects remain a distinct legacy compatibility domain and are not
backfilled or aliased to distributed projects. Phase 12 memory IDs remain
bound to local user/path/device/inode identity.

## Rationale

Filesystem identity is machine- and mount-specific. Reusing it for a logical
distributed project would couple project authorization to server paths and
could silently merge unrelated source or historical memory.

## Alternatives considered

- Derive distributed IDs from host path or display name: rejected.
- Backfill legacy projects automatically into logical projects: rejected.
- Alias `/workspace` memory to host path identity: rejected; the checked-in
  bind-mount probe found different IDs.
- Automatically merge/rekey Phase 12 memory: rejected.

## Consequences

- Explicit bindings connect project/device; snapshots bind that identity
  without storing local absolute paths.
- Memory association is preview-only; migration requires separate operator
  review, backup, dry-run, new namespace and rollback.
- Legacy APIs and logical project routes must continue to validate identity
  independently.

## Implementation dependencies

Implemented in `synai/web/projects.py`, `distributed.py`, `database.py`,
`coding_agent/memory.py`. Tests: `tests/test_web_distributed.py`,
`tests/test_web_container_memory_identity.py`,
`tests/test_coding_agent_memory.py`.

## Historical input

Phase 13B/13C docs describe the separate domains. The original plan and
approval record were not found in this repository.
