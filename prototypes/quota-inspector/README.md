# Offline quota-inspector protocol prototype

This standalone Rust package is an unprivileged, short-lived fixture harness for
the v1 `storage_inspector.py` wire contract. It is **not** a production quota
inspector, daemon, service, privilege boundary, or live storage acceptance
signal. It performs no mount, filesystem, quota, kernel, Podman, or subprocess
operations. Role paths and mount targets in its inventory are data only and are
never opened.
Do not deploy it or treat its successful fixture response as authoritative.

## Build and tests

From this directory, using an existing Rust toolchain:

```sh
cargo test
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
```

The package uses `serde`/`serde_json` for strict typed JSON, `sha2` for the
inventory digest, `getrandom` for OS randomness, and `rustix` for Unix peer
credentials and a listener with an explicitly bounded backlog. Resolved
versions are recorded in `Cargo.lock`.

## Harness

The executable takes only an operator-provided inventory file, a protected
absolute, current-user-owned 0700 parent directory for its private runtime
directory, the exact allowed peer UID/GID, and a lifetime:

```sh
cargo run --bin fixture-harness -- \
  --parent /path/to/private-parent \
  --inventory /path/to/inventory.json \
  --allowed-uid 1000 --allowed-gid 1000 --lifetime-ms 5000 \
  --backend fixture
```

`--backend native-unavailable` returns the protocol's `unavailable` status
with `native_backend_unavailable`; it never fabricates a native observation.
The default is the fixture backend. The process refuses to run as root and
requires a lifetime in `1..=30000` milliseconds. On start it creates a
randomly named 0700 directory and 0600 Unix stream socket under the selected
parent and prints one flushed JSON startup line containing the socket path,
inventory generation, random process boot epoch, and `fixture_only: true`.
No observations or later status are written to stdout. The socket and private
directory are removed on normal exit and handled errors, without recursively
deleting anything. Parent ancestors must be protected, real directories owned
by root or the current user; a root-owned sticky ancestor is allowed.

The lifetime deadline starts at executable entry, before argument parsing and
inventory loading, and is carried through initialization, startup output,
serving, and the cleanup attempt. Startup and diagnostic output use bounded,
nonblocking writes against a monotonic deadline; diagnostics are sanitized,
capped at 1024 bytes, and get just one immediate nonblocking write attempt
(including after deadline expiration). Normal serving-lifetime expiration is
successful when cleanup succeeds. Cleanup is owned by an identity-checking RAII guard,
rechecks the directory device/inode and socket identity, removes only the
original socket and then the empty private directory, and returns an
`cleanup unresolved` error rather than ignoring failed removals or substituted
identities. Repeated cleanup tolerates already-absent original resources.
Filesystem operations may still block in uninterruptible kernel or remote
filesystem paths; this in-process deadline is best-effort there, so any
harder process-lifetime bound must come from the independent external
supervisor. Same-UID replacement between identity verification and path-based
removal remains a TOCTOU limitation.

The server handles one connection at a time, uses a backlog of four, checks
Linux `SO_PEERCRED` PID/UID/GID before reading any request, and has no workers,
queue, retry loop, TCP listener, or daemon mode. It accepts at most ten
connections in any rolling one-second interval (including malformed and
unauthorized peers). The rolling window stores at most those ten admitted
connections; excess accepts are rejected without growing history. It enforces
a 500 ms total connection deadline capped by the process lifetime, and stops
by its mandatory lifetime deadline. Wire frames
are a four-byte big-endian size followed by 1..16384 bytes of UTF-8 JSON.
Requests are strictly typed, reject duplicate/unknown/missing keys, have
bounded nesting, and must bind to an allowlisted role and the immutable
inventory generation. Challenges are client-generated; the server keeps up to
1024 accepted challenges for 30 seconds using monotonic time, never evicts a
live entry, and reports replay as `inconsistent`/`challenge_replayed`.

## Inventory and compatibility

`tests/fixtures/quota_inspector_v1/inventory.json` is the canonical synthetic
fixture. The fixed input shape is a label, up to 32 ten-field storage roles,
one complete observation per role, and `expected_generation`. Inventory input
must be a no-follow regular file and is bounded to 16 KiB before parsing;
special files, symlinks, oversized files, and unstable file identities are
refused. Unknown,
missing, duplicate, ill-typed, overflowing, or inconsistent values are
rejected before listening. The fixture uses the existing `task_slot` storage
role kind. Role paths and mount targets must be canonical absolute strings but
are never resolved or accessed.

Generation matches Python's implementation: roles are sorted by `role_id`,
then the exact ten role fields are serialized as compact, key-sorted JSON with
Python-compatible `ensure_ascii=True` escaping and SHA-256 hashed. The v1
client's Python integer domain is unbounded; this producer intentionally
requires unsigned 64-bit observation counters and the narrower representable
configuration bounds checked here. This is a local harness/configuration
constraint, not a change to the v1 wire schema or client semantics.

The request and successful/unavailable response key sets, typed fields,
challenge/generation bindings, status names, UUID/mount validation,
freshness timestamp, and inconsistent usage/capacity conditions follow the
existing Python inspector contract. The fixture generation in the committed
golden input is
`dceeb2679fd90065e786f689b67ca00545c8f50bb5158f7043d03b9812a6d609`.

## Security and readiness boundary

This prototype has no production Broker configuration or deployment wiring.
Its fixture success uses the ordinary v1 `ok` wire response and therefore is
not cryptographically distinguishable from a real helper by that protocol
alone. Safety here depends on the harness being non-installed and isolated
from Broker configuration, plus independent filesystem, kernel, VFS, runtime,
and readiness gates. Protocol validation does not protect against an
administrator replacing a trusted helper. Running tests does not authorize
host changes, capabilities, privileged services, live quotas, or Phase 13F-B.
