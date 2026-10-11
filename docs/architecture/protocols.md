# SynAI 2.0 Protocol Contracts

This document separates wire contracts present in source from the planned
Core-to-Broker and patch protocols. "Implemented" means source exists; it does
not mean a production deployment has been security-reviewed. JSON schemas
must reject unknown fields where the implemented strict Pydantic schema does
so. The definitive architecture and boundary requirements are in
[synai-2.0.md](./synai-2.0.md) and [trust-boundaries.md](./trust-boundaries.md).

## Versioning and common identity rules

Implemented public routes are under `/api/v1`. Device WebSocket path is
`/api/v1/client/v1/connect`; browser event streams use
`/api/v1/events/v1/...`. Pairing, device HTTP signatures and snapshot
manifests carry their own version tags. The only supported device protocol
version is 1. Opaque IDs are 32 lowercase hexadecimal characters; content
and manifest digests are 64 lowercase hexadecimal SHA-256 hex characters.

Unknown protocol versions, identities, operation types and malformed payloads
are rejected. Timestamps have bounded freshness, device nonces are unique
within their retention period, and message sequence numbers are monotonic
within each device WebSocket connection.

## Browser-to-Core

### Implemented authentication and HTTP

- Login: `POST /api/v1/auth/login`, exact configured Origin; server issues
  HttpOnly, SameSite=Strict session cookie and a CSRF token.
- Session/CSRF: `GET /api/v1/auth/session`,
  `POST /api/v1/auth/csrf`.
- Authenticated application mutations require the cookie session, exact
  Origin and `X-CSRF-Token`; login and CSRF refresh are origin-checked
  authentication flows with their own protections. Logout/password rotation
  revoke sessions as implemented.
- No wildcard CORS. Configured public origin controls secure-cookie behavior.
- Errors use bounded structured error data; Core request/body/concurrency
  limits are enforced by the FastAPI layer.
- The service is single-user. A valid session currently represents the
  single configured operator and is not a per-project ACL.

Implemented project/device/snapshot routes include:

| Operation | Route | Auth |
|---|---|---|
| Legacy configured project registration/list/inspect | `/api/v1/projects...` | Browser session; registration is CSRF-protected |
| Pairing challenge | `POST /api/v1/devices/pairing-challenges` | Browser session + CSRF |
| Logical project and binding metadata | `/api/v1/logical-projects...` | Browser session; writes require CSRF |
| Device list/authorize/revoke | `/api/v1/devices...` | Browser session; writes require CSRF |
| Request a client snapshot | `POST /api/v1/logical-projects/{project_id}/bindings/{binding_id}/snapshot-requests` | Browser session + CSRF; sends a request, not approval |
| Project snapshot metadata | `/api/v1/logical-projects/{project_id}/snapshots...` | Browser session |
| Agent Task metadata / target availability | `/api/v1/logical-projects/{project_id}/tasks`, `/api/v1/execution-targets` | Browser session; execution unavailable |

### Implemented chat and browser event streams

`/api/v1/chat/...` creates/lists/reads conversations, starts turns and
cancels active turns. Browser Chat runs the existing `Agent` with tool
execution disabled; model-emitted tool calls are rejected, not dispatched.
The authenticated chat WebSocket uses a version-1 event envelope with a
monotonic durable conversation cursor. Project activity uses durable project
cursors. Reconnect may replay events or require state refresh; APIs and
persisted records remain authoritative. Neither stream re-submits a turn or
confers source/execution rights.

WebSocket inputs are bounded and browser event sockets validate session and
Origin. Current subscriber accounting is process-local and the service
supports one data-root owner process.

## Client-to-Core pairing and authentication

### Pairing (implemented)

1. Authenticated operator requests a five-minute pairing challenge. Secret is
   returned once with `Cache-Control: no-store`.
2. Client creates Ed25519 private key locally and signs:

   ```text
   "synai-device-pairing-v1\n" || canonical_json({
     challenge_id, challenge_secret, public_key(base64),
     protocol_version, capabilities
   })
   ```

3. Core verifies signature, challenge secret/expiry and supported protocol,
   then consumes the challenge transactionally and creates a `pending`
   device.
4. A separate authenticated operator action authorizes or revokes it.

### Signed device HTTP requests (implemented)

Headers identify device, credential, timestamp, nonce and signature. The
signed bytes are:

```text
synai-device-request-v1\n
{UPPERCASE_METHOD}\n
{quoted_path_without_query_or_fragment}\n
{unix_timestamp}\n
{nonce}\n
{sha256_hex(exact_body_bytes)}
```

Ed25519 verification uses the paired public key. Credentials are random,
stored as hashes, expire after 90 days and are revoked/rotated independently.
Server validation enforces authorized device state, timestamp skew (60
seconds), nonce replay rejection and request/body correlation. Query
parameters are not part of the device signature; signed device endpoints
must not introduce security-sensitive query semantics without revising this
contract.

The client helper retries certain HTTP network/timeouts using a fresh nonce.
Upload begin has an idempotency key, identical chunks are idempotent and
commit returns the same snapshot. This is not a general exactly-once
guarantee; a future side-effecting endpoint must define its own durable
idempotency and ambiguous-result recovery.

## Client-to-Core authenticated WebSocket (implemented)

Client connects outbound over WSS for remote Core deployments. `hello` is
sequence 0 and is signed over canonical schema-version/type/device/timestamp/
nonce/sequence/payload fields through the device request signature scheme.
The server checks credential, signature, freshness, protocol and capabilities.
Subsequent signed client frames use strict increasing per-connection
sequences and unique fresh nonces. Accepted client operations are heartbeat,
status, disconnect and acknowledgments for bounded snapshot operations.
Unknown messages close the socket. Presence is in-memory and is not durable
authority. A new connection replaces the previous device connection.

Server messages carry schema version, type, device ID, sequence and payload.
Client checks exact schema, identity, sequence and required payload fields.
Server messages are **not independently signed**; authenticity/confidentiality
rely on validated TLS/WSS to the configured Core origin. Snapshot requests
are transient, device/binding/project-correlated, and expire within ten
minutes. They only prompt a local preview/consent flow.

## Snapshot upload protocol

### Implemented HTTP operations

- `POST /api/v1/device/{device_id}/snapshot-uploads`: signed device request;
  binds project ID, binding ID, idempotency key and bounded file list.
- `PUT /api/v1/device/{device_id}/snapshot-uploads/{upload_id}/files/{file_index}/chunks/{chunk_index}`:
  signed chunk; ordered, bounded, exact chunk lengths.
- `POST .../{upload_id}/commit`: signed commit after all data is present.
- Device-authenticated status routes expose upload state; browser routes
  expose snapshot metadata/manifest only.

The client has limits of 500 files, 1 MiB/file, 64 MiB total, 16 path
components, 512 characters and chunks no larger than 256 KiB. Core limits are
server-controlled (default policy values are in
`synai/web/snapshots.py`); hard validation ceilings are separately enforced.
There are active upload/storage quotas and retention. No archives are
accepted or extracted.

### Manifest version 1

Core commits a canonical UTF-8 JSON manifest. Its implemented shape is:

```json
{
  "schema_version": 1,
  "project_id": "32-lowercase-hex",
  "source_device_id": "32-lowercase-hex",
  "workspace_binding_id": "32-lowercase-hex",
  "snapshot_id": "32-lowercase-hex",
  "created_at": 0,
  "files": [
    {
      "path": "relative/portable/path",
      "file_type": "regular",
      "size_bytes": 123,
      "sha256": "64-lowercase-hex"
    }
  ],
  "total_bytes": 123,
  "source": {
    "kind": "paired_device",
    "key_fingerprint": "64-lowercase-hex",
    "protocol_version": 1
  }
}
```

The client supplies per-file expectations only. Core validates file paths,
type, size, bytes, UTF-8, extension and digest, constructs the final identity
and source fields, computes total bytes, canonicalizes and hashes the
manifest. The manifest digest binds snapshot identity and file-list metadata;
each object digest binds exact content. The read-only `SnapshotSource`
revalidates manifest and object digests before indexing.

### Error and cancellation behavior

Invalid, oversized, incomplete, reordered or digest-mismatched content is
rejected with explicit bounded API errors. Identical retransmission is
accepted as duplicate; conflicting bytes for the same chunk or idempotency
key are conflicts. Snapshot expiry returns an explicit expired/unavailable
error rather than fallback content. The local client does not automatically
resume an uncertain transfer after process restart; the user must create a
fresh preview and consent. WebSocket disconnect or expired operation does
not imply upload completion.

## Legacy task and execution contracts in source

`synai/web/distributed.py` defines typed task, target and execution-claim
records, and SQLite has a `distributed_tasks` table containing task,
snapshot/device/binding, state, target, claim, fencing generation, approval
and result/error references. A helper can persist a disabled identity-only
task record. The browser can list task records and target availability.
Current response/API explicitly sets `execution_available: false` and
`broker_available: false`.

These typed fields are **not a protocol**: no task creation, claim, renewal,
release, cancel, recovery or Core-to-Broker endpoint exists; there is no
production claim signer/ledger or independently enforcing Broker. Existing
workspace lease generations are for Phase 13B configured workspace
coordination and are not automatically the distributed task fencing source.

## Planned Core-to-Broker protocol (not implemented; no version is approved)

The 13F-A protocol must not silently broaden authority. Before a wire version
is approved, design review must specify:

- mutually authenticated Core/Broker service identities and transport;
- canonical versioned request/response schemas that reject unknown fields;
- a durable, one-shot task claim bound to task ID, project ID, exact snapshot
  ID and manifest digest, source binding/device, execution target and
  Broker-owned allocation;
- claim issue/expiry, unique request/idempotency ID, fencing generation,
  policy decision and scoped approval references, exact allowed operations,
  server-controlled resource profile and absolute deadline;
- authenticated cancellation bound to the claim, plus status lookup for
  ambiguous results; no retry of a request that may have started;
- Broker-side independent validation immediately before container side
  effects and at supported mutation boundaries;
- retrieval of only the exact immutable Core snapshot over a narrow
  read-only interface, with manifest/object re-hash before use;
- bounded typed results/artifacts with request/task/claim/generation/snapshot
  correlation, digest and provenance checks;
- explicit interrupted/indeterminate status and recovery evidence proving
  previous writers are reaped before a new fence/claim;
- server-side image allowlist with pinned digests, no caller runtime flags,
  no Core socket, network disabled by default, and fixed mount/limit policy.

No proposed JSON field list in this paragraph is an implemented field or
approved contract. In particular, an ID, generation number or approval
reference in a payload is not authority unless Broker verifies its source,
scope, freshness and current status independently.

## Artifact/result validation and future patch receipts

No distributed result or artifact wire schema exists. The Broker must
validate task and claim correlation, exit/cancellation state, size/count
bounds, output encoding, object digest and source snapshot provenance before
Core records success. Core must reject uncorrelated or stale responses and
never treat process exit as proof of task intent or source changes.

No client patch protocol exists. A future artifact must bind task, claim,
snapshot/digest, allowed relative paths, patch digest and review/approval
provenance. The Client Agent must verify all fields and local preimages,
show the exact change, request new local consent, apply only within its
registered workspace, and return a receipt with before/after digests,
per-path outcome, conflict/partial state and timestamp. Browser login,
device pairing, prior upload consent and task completion cannot cause
automatic application. Failure/uncertainty must not force-apply or replay.
