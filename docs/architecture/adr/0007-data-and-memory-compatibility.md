# ADR 0007: Historical Data and Memory Compatibility

Status: Accepted for historical data compatibility.

## Decision

Keep historical conversation/task formats and opt-in Phase 12 project-memory
records readable. Memory association or migration requires explicit review,
verified backups, and a reversible process. Do not restore retired launchers,
editor assets, interface-specific dependencies, or execution fallbacks.

## Consequences

Preserve the shared agent, history, indexing, planning, review, verification,
policy and data-integrity logic, including single-process data-root ownership
and recovery protections. Core must not gain host execution authority.

Retirement of the previous local interfaces is recorded in Git history.
