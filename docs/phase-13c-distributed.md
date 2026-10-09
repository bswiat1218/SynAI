# Phase 13C distributed foundation

Phase 13C adds server-owned logical project identity, paired-device enrollment
contracts, bounded immutable source snapshots, and a read-only adapter into
the existing repository intelligence. It does not add a Linux Client Agent,
Sandbox Broker, planner, task runner, AI-directed task execution, or workspace
mutation API. Device filesystem consent must be implemented by the future
installed Client Agent; the API in this phase never requests local commands,
shell access, patches, mounts, or host paths.

The Phase 13B path-based `projects` table and `ProjectRegistry` remain the
legacy compatibility domain. They are not backfilled, aliased, or granted
distributed authority; legacy project API responses label records with
`compatibility_state: "legacy_host_path"`. New logical projects are held in
separate tables and are identified only by random server-generated 128-bit
opaque IDs. Device,
workspace-binding, snapshot, task, execution-target, and future broker
allocation identifiers use separate types and namespaces. The project
registration idempotency key is a client retry key, not a project identity or
filesystem locator.

## Database and trust boundaries

Metadata schema version 3 is an additive, exclusive SQLite migration from
Phase 13B version 2. It leaves credentials, browser sessions, path-bound
projects, and workspace lease generations untouched and adds separate
logical-project, pairing, device, binding, snapshot, upload, task, replay
nonce, and memory-association tables. Unknown schemas still fail closed.

The browser continues to authenticate with its HttpOnly session cookie and
CSRF token. Device credentials use a separate header namespace and are
accepted only with a request signature from the paired Ed25519 key. Browser
sessions cannot authenticate device routes; device credentials cannot
authenticate browser routes. The Core sends no requests to client devices;
future agents must initiate outbound authenticated connections.

The API is currently single-operator, as is the Phase 13B browser service.
Operator authorization is therefore enforced by an authenticated,
CSRF-protected browser action; multi-user project ACLs are not implemented.
Every binding explicitly joins one logical project to one authorized device.
Snapshot upload checks all three IDs (project, device, binding) on the server.
Bindings can be revoked or assigned a bounded expiry; an expired binding is
reported stale and cannot supply new snapshots.

## Pairing and device authentication

Protocol version 1 is the only supported version. An operator creates a
pairing challenge with a five-minute expiry. The challenge secret is returned
once to the authenticated operator for secure out-of-band transfer. The test
client generates its own Ed25519 private key and signs the canonical
`synai-device-pairing-v1` payload containing the challenge ID and secret,
public key, version, and bounded capability advertisement.

Successful proof consumes the challenge transactionally and creates a
`pending` device. It does not authorize the device. An operator must explicitly
authorize that pending device before its credential can be used. The
single-use challenge plus transaction-serialized enrollment prevents replay
and concurrent double enrollment.

Authorized requests require a random bearer credential, device ID, Unix
timestamp, one-time random nonce, and Ed25519 signature over protocol tag,
HTTP method, URL path, timestamp, nonce, and SHA-256 body digest. The server
accepts at most 60 seconds of clock skew, stores consumed nonces, and rejects
credential expiry/revocation, invalid proof, and nonce replay. Credentials
expire after 90 days. A device may rotate its credential over a valid signed
request; rotation invalidates the previous credential immediately. The
connected indication is derived from recent authenticated traffic (120
seconds), not from a claimed client status. Capabilities are metadata only;
this version advertises only snapshot/status protocol features.

No production Client Agent is included. Tests use generated disposable
Ed25519 keys as an explicitly identified test client. Enrollment does not
provide file access until a future real agent independently implements
preview, local user consent, collection, and the outbound protocol.

## Snapshot format, limits, and privacy

The snapshot manifest is canonical UTF-8 JSON (sorted keys, compact encoding),
schema version 1:

```json
{
  "schema_version": 1,
  "project_id": "server-generated-id",
  "source_device_id": "server-generated-id",
  "workspace_binding_id": "server-generated-id",
  "snapshot_id": "server-generated-id",
  "created_at": 0,
  "files": [
    {
      "path": "src/module.py",
      "file_type": "regular",
      "size_bytes": 123,
      "sha256": "64 lowercase hexadecimal characters"
    }
  ],
  "total_bytes": 123,
  "source": {
    "kind": "paired_device",
    "key_fingerprint": "64 lowercase hexadecimal characters",
    "protocol_version": 1
  }
}
```

The Core constructs the final manifest, recomputes each file length and
SHA-256, derives total size and source key fingerprint, and hashes the
canonical manifest. Client-declared sizes/hashes are expectations only.
Uploaded files must be regular UTF-8 text files with a repository-index
supported source/text extension; per-file bytes are capped at the Phase 2
index's existing 1 MiB limit. General archives are unsupported and never
extracted. The protocol transfers individual path-and-byte objects, not a
filesystem archive.

Initial server-owned limits are: 500 files, 1 MiB/file, 64 MiB/snapshot,
16 path components, 512 path characters, 256 KiB/chunk, four concurrent
uploads/device (64 globally), 256 MiB active-upload storage, 1 GiB total
retained snapshot storage, one-hour upload deadline, 30-day retention, and
the existing bounded HTTP body/concurrency limits. Tests may inject lower
limits. Any limit change remains server-controlled and may not exceed the
hard validation ceilings.

The server rejects absolute/traversal/backslash/control paths, duplicate
paths, case-folded or Unicode-normalization collisions, non-regular types,
unsupported extensions, invalid UTF-8, NUL bytes, per-file/total overages,
bad chunk order/length, conflicting duplicate chunks, incomplete uploads,
and content digest mismatch. Existing repository-index generated/cache/VCS
directories are excluded, along with common environment, credential, private
key, and certificate paths. These are conservative path rules, not secret
detection: heuristic inspection cannot guarantee sensitive data has been
removed. A future Client Agent must show an inclusion preview before upload
and obtain local user consent.

Snapshot metadata is in private Core metadata storage. Temporary chunks,
committed immutable objects, and disposable source views are in the dedicated
private `~/.synai/web-snapshots/` tree, separate from authentication SQLite,
conversation history, Phase 12 project memory, and any future Broker runtime.
Temporary directories/files are owner-only; committed objects and manifests
are read-only and atomically renamed into their final namespace before the
metadata transaction commits. A crash before metadata commit leaves an
orphaned directory that startup reconciliation removes. A periodic and
upload-triggered collector expires abandoned uploads and retention-expired
snapshots. It preserves snapshots referenced by queued/claimed task records.
Expired or unavailable snapshot reads return explicit `410` errors; they are
not replaced with another source.

Upload creation has a device-scoped idempotency key. Repeating an identical
begin request returns the existing upload; conflicting reuse is rejected.
Identical chunk retry is idempotent; different bytes at the same index are a
conflict. Commit is idempotent and returns the same committed snapshot. Device
request nonce replay remains independently rejected.

## Repository intelligence adapter

`SnapshotSource` validates the stored manifest digest, rehashes every
committed object, creates a Core-owned non-executable read-only source view
when needed, and revalidates view paths through the existing
`RepositoryIndex.read_file_bytes` no-follow reader. It passes only that
directory to the existing Phase 2 index and Phase 3 `ContextEngine`; it does
not import or execute project modules. Source bytes remain verified against
the committed manifest. No live host workspace mount is used.

The adapter intentionally preserves Phase 2's assumptions: UTF-8 text,
allowlisted extensions, 1 MiB/file, 64 MiB/index scan, and existing excluded
directories. Unsupported content is rejected during snapshot ingestion rather
than silently presented as fully indexed. Phase 5 execution, writable
checkouts, Phase 9 task attribution, and snapshot-backed memory retrieval are
not enabled.

## Tasks, targets, and memory compatibility

The versioned distributed task record carries project ID, immutable source
snapshot, device/binding provenance, selected execution-target identity,
capability requirements, state, execution claim, fencing generation,
approval reference, and result/error references. Schema types distinguish
execution targets from future Broker allocation IDs. The read-only task API
returns `execution_available: false`; no task-creation or execution endpoint
exists. `CodingAgentRuntime` remains the only implemented task engine.
Server-side lease generations are orchestration metadata, not a client
filesystem lock.

Phase 12 memory identity derivation and stored records remain unchanged.
Schema v3 includes a versioned association table as a future audit/migration
contract, but Phase 13C creates no association rows. The authenticated
CSRF-protected preview validates an opaque legacy identity and bounded
provenance, returns `preview_only`, and does not open, enumerate, copy, merge,
or rekey a memory database. Actual reassociation remains disabled. Any later
migration needs an independently verified source, explicit operator
authorization, backup of the original database, copied-database dry run,
identity/provenance comparison, transactional writes into a new namespace,
and tested rollback. Legacy evidence is never automatically fresh for a new
binding; project memory remains opt-in.

## API and future protocol boundary

New `/api/v1` routes cover logical project metadata, operator pairing
challenges, device enrollment/authorization/revocation/capabilities,
workspace bindings, device-only credential rotation and signed snapshot
upload/status/commit, browser snapshot manifest/status, read-only task status,
and memory-association preview. They do not accept host paths, shell
commands, mounts, container options, arbitrary file edits, or a Broker
allocation. Snapshot bytes enter Core's bounded temporary store; no upload
route forwards to a Broker.

The future Client Agent must initiate outbound connections, use the separate
device key/credential namespace, implement local preview/consent, and support
the versioned pairing, signed request, binding, and snapshot messages above.
The future Broker must have its own service identity, fixed runtime policy,
resource limits, replay-safe task claims, and independently enforced leases.
Neither future service is present in this implementation. The generated
OpenAPI contract and TypeScript schema are checked in as
`web/openapi.json` and `web/src/api/schema.d.ts`.

## Core isolation evidence and limitations

Distributed project and snapshot routes use only logical IDs and Core-owned
private storage. The existing legacy project registration remains
read-only. No new host execution backend, runtime socket, Docker/Podman
dependency, or writable host-project route is added. This is code and test
evidence, not proof of absolute host security or a container-deployment
review. A production Core image/mount/UID deployment has not been defined in
Phase 13C; verify those properties before deployment. No device-side command
execution, Broker, distributed task execution, or UI control is implied.

The opt-in `tests/test_web_container_isolation.py` uses the minimal disposable
fixture in `tests/fixtures/phase13c-core-isolation/`; run it with
`SYNAI_RUN_CONTAINER_TESTS=1`. It inspects the created container's mounts,
read-only root, non-root UID, dropped capabilities, network setting, resource
limits, runtime-socket absence, and write access to only its two dedicated
private data volumes. It intentionally does not mount the checkout or a
project workspace and is not a substitute for inspecting a future production
Core image or deployment.
