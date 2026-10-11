# Historical Phase 13 Plans

These files are preserved verbatim as historical design inputs. They are not
the current architecture specification and do not override newer decisions
or implementation evidence. The current reference remains
[the SynAI 2.0 architecture baseline](../synai-2.0.md).

| Historical document | Provenance | Integrity |
|---|---|---|
| [Phase 13A Web Application Migration Plan](./Phase13A-WebApplicationMigrationPlan.md) | Supplied read-only source attachment; archived without content edits. | SHA-256 `c47ef0fc8cf25a336aabff0725125d35477f569ac81ab15ee930b1af81bc8e83` |
| [Phase 13 Architecture Realignment](./Phase13ArchitectureRealignment.md) | Supplied read-only source attachment; archived without content edits. | SHA-256 `b3401a8a2156c34542c3f51e71e3484c9522304c3202d8333227a6f00e60660e` |

The supplied documents are plans, not independent approval records. The
realignment describes rootless Podman as its initial supported Broker runtime,
but the source material does not include a separate sign-off proving that
specific implementation decision was formally approved at that time. The
operator subsequently approved the initial Broker architecture for Phase
13F-A design on 2026-10-10; that current approval is recorded in
[ADR 0004](../adr/0004-rootless-podman-broker.md) and does not retroactively
change historical provenance or constitute implementation/security
acceptance.

## Traceability and supersession notes

- The Phase 13A plan describes a web/Core read-only mount of registered
  project roots and a separate runner that receives the selected workspace
  read-write. The later realignment changes the distributed source model to
  immutable Client Agent snapshots and Broker-managed task data; it explicitly
  says Core never mounts client projects or host workspace directories. The
  current architecture follows the later snapshot-only boundary. The earlier
  host-path mount design is retained here as historical context, not a
  compatible fallback.
- The Phase 13A roadmap places runner implementation in 13C and broad parity/
  rollout gates in 13F. The realignment moves Broker execution and Agent Task
  integration to 13F, client patch application to 13G, and production
  deployment hardening to 13H. The current 13F-A/13F-B split further
  decomposes the realignment's 13F work into implementation and acceptance.
  This is roadmap supersession, not a change to an implemented protocol.
- The realignment specifies rootless Podman, a dedicated unprivileged Broker
  identity, private runtime ownership, no network by default, digest-pinned
  images, bounded task containers and no Core runtime socket. ADR 0004
  carries these constraints forward and has separate current architecture
  approval; the historical plan itself is not that approval evidence.
- Both plans retain legacy TUI support, separate browser chat from tasks,
  prohibit silent host-execution fallback and require no replay of uncertain
  operations. These points align with the canonical current architecture.
- The realignment sketches a later Core-to-Client patch offer and local apply
  flow (13G). It does not authorize automatic application. Current
  architecture continues to require independent local review/consent and
  treats patch application as future work.

No historical plan is silently amended by this index. Any further
supersession should be recorded in the canonical architecture or an ADR.
