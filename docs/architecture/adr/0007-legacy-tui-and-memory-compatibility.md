# ADR 0007: Preserve legacy TUI and historical memory compatibility

- **Status:** Accepted compatibility invariant; existing formats and
  behavior retained. Historical approval record is absent.
- **Date:** 2026-10-10 baseline documentation.

## Decision

The existing Linux TUI and Phases 0–12 behavior remain separately supported
under their original security semantics. Conversation, task/checkpoint and
Phase 12 memory schemas are not changed by the distributed architecture
baseline. The web service and TUI share the existing exclusive data-root
ownership lock. No automatic history migration, memory merge, identity alias
or checkpoint reinterpretation is allowed.

## Rationale

The existing local workflow has established execution, approval, recovery
and memory behavior. Distributed identities and Core storage have different
trust and path semantics; transparent merging risks changing authorization or
historical provenance.

## Alternatives considered

- Replace TUI with Browser Chat: rejected.
- Allow concurrent Core/TUI writes against the same data root: rejected by
  existing ownership lock.
- Rewrite stored task/checkpoint/memory records: rejected in this phase.
- Automatically associate a Core project with a Phase 12 memory namespace:
  rejected.

## Consequences

- Legacy execution can remain available locally, but is not evidence of
  Broker isolation.
- Web and TUI cannot own the same data root concurrently.
- Phase 12 memory stays opt-in and path/device/inode/user-bound.
- Any future memory association requires a separate reviewed, backed-up,
  copied-database dry run and rollback plan.

## Implementation dependencies

Implemented in TUI entrypoint, `synai/web/ownership.py`,
`synai/coding_agent/memory.py` and history/checkpoint validators.
Representative tests: `tests/test_storage.py`,
`tests/test_coding_agent_memory.py`, `tests/test_agent_state.py`,
`tests/test_web_container_memory_identity.py`.

## Historical input

The checked-in architecture baseline documents existing Phases 0–12 and 13B
compatibility. No original approval record was present. No historical schema
was modified to create this ADR.
