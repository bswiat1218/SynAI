# ADR 0002: Client Agent authorization and local consent

- **Status:** Accepted for snapshot reads; implemented in Linux Client Agent.
  Future modifications require a new decision. Historical approval record is
  absent.
- **Date:** 2026-10-10 baseline documentation.

## Decision

Device identity and server-side project binding do not authorize local
workspace access. A local user registers an explicit local binding to a
directory, authorizing the Client Agent's bounded reads needed to build a
preview. For each snapshot, the Agent validates that binding, captures the
preview and requires separate explicit local consent before transmitting
those exact bytes to Core. Browser login or a Core snapshot request cannot
create either local read authorization or upload consent. Current Client
Agent has no project write, shell or patch application capability.

## Rationale

Core cannot authoritatively grant permission on a remote user's filesystem.
Separate local consent prevents a browser account, stolen server session or
device credential from silently reading or modifying client files.

## Alternatives considered

- Treat server binding/device authorization as sufficient file permission:
  rejected.
- Enable unattended service consent: rejected; non-interactive capture
  denies.
- Use browser-session credentials on the device: rejected; authentication
  namespaces remain separate.

## Consequences

- User sees relative paths, exclusions, byte counts and a disclosure warning
  after the authorized local scan has read the candidate file bytes.
- Client transfers bounded individual file objects and never sends local
  absolute paths.
- Heuristic exclusions do not guarantee secret removal.
- Any future patch application needs a new local authorization, exact preview,
  conflict policy and durable receipt.

## Implementation dependencies

Implemented sources: `synai/client/cli.py`, `protocol.py`, `workspace.py`,
`state.py`; tests: `tests/test_client_agent.py`. Future local writes require
a separately reviewed patch schema, preimage checks, consent and receipt.

## Historical input

Checked-in Phase 13E notes document preview, consent, local key custody and
noninteractive denial. No original Phase 13A/realignment record was found;
this ADR does not invent one.
