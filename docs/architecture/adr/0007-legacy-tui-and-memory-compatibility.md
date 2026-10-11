# ADR 0007: Retired User Interfaces and Historical Data Compatibility

Status: Superseded by the operator decision on 2026-10-10 to retire the original interactive interface and bundled editor.

## Decision

Only the React web application and the Linux Client Agent administration CLI are supported interactive interfaces. The old interface and editor have no supported launchers, imports, execution fallbacks or release assets. Preserve historical conversation and task data formats and opt-in Phase 12 memory through explicit, verified migration only.

## Consequences

Remove interface-specific dependencies, source, tests, packaged assets, and stale documentation. Preserve shared agent, history, indexing, planning, review, verification, policy and data integrity logic, including data-root ownership and recovery protections. Core may not gain host execution authority as a side effect of this retirement.

The previous co-existence decision is superseded and remains visible in Git history.
