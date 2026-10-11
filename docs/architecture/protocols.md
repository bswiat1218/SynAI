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

## Proposed Core-to-Broker claim and fencing contract

**Status:** proposed `synai-broker/v1`; not implemented or approved. These
fields and semantics specify a reviewable design baseline, not an existing
wire format. Core/Broker requests use mutually authenticated service
identities over a protected channel. Schemas are canonical, size-bounded and
reject unknown fields. Every request carries protocol version and a unique
request ID.

### Claim payload

Core issues a signed, short-lived, task-scoped execution claim after its
policy/approval decision. The proposed v1 claim binds all of:

```json
{
  "protocol_version": "synai-broker/v1",
  "issuer": "synai-core:<deployment-id>",
  "audience": "synai-broker:<allocation-id>",
  "claim_id": "<opaque-id>",
  "project_id": "<logical-project-id>",
  "task_id": "<task-id>",
  "snapshot_id": "<immutable-snapshot-id>",
  "snapshot_manifest_sha256": "<64-hex>",
  "broker_allocation_id": "<broker-allocation-id>",
  "operation_id": "<unique-operation-id>",
  "operation_kind": "<allowlisted-kind>",
  "policy_provenance": {
    "policy_id": "<trusted-policy-id>",
    "policy_version": "<version>",
    "decision_digest": "<digest>"
  },
  "approval_provenance": {
    "approval_id": "<approval-id-or-null>",
    "approval_digest": "<digest-or-null>"
  },
  "plan_provenance": {
    "plan_id": "<trusted-plan-id>",
    "plan_digest": "<digest>"
  },
  "resource_profile_id": "<broker-configured-profile>",
  "image_profile_id": "<operator-approved-profile>",
  "lease_generation": 1,
  "fencing_token": "<unpredictable-token>",
  "issued_at": "<unix-time>",
  "expires_at": "<unix-time>",
  "replay_nonce": "<unique-random-value>",
  "request_id": "<unique-id>",
  "signature": "<signature-over-canonical-claims>"
}
```

This is a required binding list, not a finalized encoding or selected
signature algorithm. Values including operation kind, policy/approval/plan
references, resource profile and image profile must resolve to trusted,
Core/Broker-controlled records; caller-provided free-form instructions cannot
expand authority. The Core is the proposed issuer and durable source of
authoritative task lease generations, issued claim IDs, revocations and
operation states. Browser and Client Agent do not issue Broker claims.

### Current authority, fencing and operation semantics

1. Before issuing a claim, Core atomically allocates the next durable lease
   generation for the project/task execution scope and records its snapshot,
   operation ID, allocation identity, expiry and trusted authorization
   provenance. It does not grant a new generation while an earlier writer
   might still be running.
2. Before any dispatch or side effect, Broker verifies signature, issuer,
   audience/allocation, expiry, replay nonce, schema, scope and its own fixed
   policy. It then performs an authenticated online current-authority check
   with Core and confirms the generation is current and not revoked. A valid
   signature alone never proves current authorization.
3. Broker persists the highest accepted generation and operation ledger
   durably before side effects. Lower generations and revoked/expired claims
   fail closed. If Core cannot confirm current authority, Broker must not
   start or resume dispatch. Broker restrictions remain independently
   enforced regardless of Core authorization.
4. The same operation ID/request ID with identical canonical claim content
   returns its durable current state/result and does not repeat side effects.
   Reuse with different content is a conflict. Distinct operation IDs do not
   make an uncertain operation safe to replay.
5. Proposed lifecycle: `issued -> accepted -> running -> completed`,
   `cancelled`, `failed` or `uncertain`. Terminal states do not return to
   running. A cancel request is authenticated, claim/operation/generation-
   bound and durably revokes future dispatch before process termination is
   attempted. Cancellation response distinguishes cancellation requested
   from termination verified.
6. A newer generation cannot authorize a competing writer until the old
   operation is terminal and its processes/container are verified stopped.
   If stop status cannot be established, old and new execution remain fenced
   from mutation; escalate for operator recovery rather than allowing
   concurrent writers.
7. Only operations proven not to have started may be retried automatically.
   Idempotent reads/status queries may be retried. Create/start/exec,
   mutation, cancellation with ambiguous outcome, result commit and removal
   are not automatically retried after an uncertain response. Query durable
   status and reconcile; never replay the side effect based only on timeout.

### Restart and partition behavior

- **Core restart:** durable generations, revocations and operation records
  are authoritative and must recover before issuing claims. If integrity or
  freshness cannot be established, issuance is disabled.
- **Broker restart:** load its durable operation/allocation registry; verify
  runtime identity and ownership and reconcile actual state before acting.
  An operation whose execution state cannot be proved becomes `uncertain`;
  do not re-execute it.
- **Podman restart/crash:** inspect only Broker-owned allocations and
  containers. Reconcile stopped/completed evidence; any ambiguous process or
  writable state remains fenced until verified stopped and recorded.
- **Core disconnect/partition:** no new claims, current-authority checks,
  snapshot grants or side-effect dispatch. An already running operation may
  be terminated under local fixed policy/deadline, but its status remains
  uncertain until reconciled. Core must not issue a higher generation while
  a prior writer could survive the partition.
- **Cancellation:** Core records revocation before sending cancellation.
  Broker rejects later dispatch for that operation even if a previously
  signed claim remains unexpired. Reconnect requires status reconciliation,
  not replay.

These rules intentionally trade availability for single-writer safety.
The exact durable transaction boundary, lease scope, signing/key-rotation
mechanism, online-check caching policy (proposed default: no authorization
caching for side effects), and recovery evidence format require design
approval.

## Proposed Broker snapshot retrieval

There is no direct Client Agent-to-Broker upload and no arbitrary host
filesystem retrieval. The Broker fetches input only through a narrow,
read-only Core service interface after claim validation:

1. Broker authenticates as its dedicated service identity using mutually
   authenticated transport and a service credential independent of browser
   sessions/device keys.
2. Core issues a short-lived retrieval grant scoped to one claim, project,
   task, snapshot ID, manifest digest, Broker allocation and byte/file limits.
   Grant ID, issue/expiry times and replay nonce are recorded. Grant does not
   authorize other Core state or other snapshots.
3. Broker fetches only manifest-listed immutable objects through opaque
   snapshot object IDs; no path, arbitrary file URL, database, upload
   staging directory or host path is accepted as a retrieval selector.
4. Broker validates the canonical manifest digest against the claim, enforces
   file/count/total-byte/path/depth limits, then recomputes every object
   digest and size. Any mismatch, unexpected object or limit violation
   rejects the whole input before execution.
5. Transfer stages in a private per-allocation directory with restrictive
   permissions and no shared writable paths. Partial transfers are
   discarded on error, expiry, cancellation or restart unless an
   integrity-checked resumable scheme is separately approved. Staged input
   becomes immutable to the workload; writable task data is a distinct
   bounded layer.
6. Grants expire, are claim/allocation-bound, and are revocable. Revocation
   prevents further object fetches. Core disconnect or failed current-
   authority check fails closed; no fallback to a client, host path, newer
   snapshot or unrelated Core store is permitted.
7. Broker removes partial/staged inputs on failure, expiry, cancellation and
   retention deadline, preserving only explicitly authorized bounded result
   artifacts and audit evidence.

Transfer concurrency, chunk size, per-file/aggregate caps, retention period,
grant signing and whether integrity-checked transfer resume is allowed are
deployment parameters requiring approval. They must be bounded in Broker and
Core independently.

## Proposed Broker allocation ownership and operation contract

The Broker keeps a durable private allocation registry mapping operation and
allocation IDs to the claim digest, fencing generation, snapshot digest,
runtime identity, container ID, expected image digest, configured limits,
state and timestamps. The registry is private to the Broker identity and is
not derived from caller-controlled container names or labels.

Before every runtime operation—create, inspect, command execution, result
collection, cancellation or removal—the Broker independently verifies the
configured rootless runtime identity, private storage/socket location,
allocation record, container ID, ownership metadata, expected image digest
and current operation state. Missing, conflicting or foreign ownership
evidence fails closed; never inspect/control an arbitrary runtime object.
Creation uses fixed Broker-generated names/labels and only approved image,
mount, namespace, network and resource profiles. Command invocation accepts
only a claim-authorized operation kind and bounded validated arguments.

On restart, reconcile registry records with runtime state before accepting
work. Verify process termination and container ownership before cleanup.
Remove only verified Broker-owned task containers and staged data. If
ownership or execution status is ambiguous, quarantine/fence the allocation
for operator reconciliation; do not remove an unrelated object or dispatch a
replacement writer.

The proposed bounded result to Core binds protocol version, project/task,
claim and operation IDs, lease generation/fencing token, snapshot ID and
manifest digest, Broker allocation identity, and includes:

- terminal/interrupted status and exit/result status;
- bounded stdout/stderr with truncation indicators and encodings;
- changed-file manifest with normalized relative paths, file types and sizes;
- preimage/postimage hashes for each reported path;
- proposed diff/artifact references with content digests and size bounds;
- input manifest/object verification status;
- artifact and provenance validation status;
- resource-limit observations/violations, timeout, cancellation and
  interruption evidence;
- runtime/container ownership and cleanup/recovery evidence.

Core verifies claim/operation/snapshot correlation, authorization freshness,
response bounds, object hashes, manifest consistency and artifact references
before recording results. A returned success string, exit code, or valid
artifact digest alone does not prove that intended filesystem changes
occurred or that execution was benign. Distinguish byte integrity from
behavioral correctness, which remains unproven without independent checks.

All contract details in these subsections are proposed and require review;
none are currently emitted or enforced by source code.

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
