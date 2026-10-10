# Phase 13E — Linux Client Agent

Phase 13E adds an unprivileged Linux Client Agent for explicitly authorized,
read-only project snapshots. The Core keeps the existing Phase 13C Ed25519
pairing, signed device HTTP authentication, workspace-binding identities,
snapshot manifest, chunk upload, and immutable storage contracts. The Agent
uses an outbound version-1 WebSocket for authenticated presence and
client-confirmed snapshot requests; snapshot bytes still travel only through
the existing signed HTTP upload API. The WebSocket is not a command channel.

The Agent runs as the invoking user, opens no listening socket, imports no
Textual/TUI, tool-dispatch, sandbox, container, or host-execution modules, and
does not modify a project. Browser chat remains tool-disabled. No Broker,
runtime, task execution, shell, patch, project write, or automatic service
installation is included.

## Install

Python 3.12 or newer is required. From a source checkout, create a dedicated
virtual environment and install SynAI's Python distribution there:

```sh
python3 -m venv ~/.local/venvs/synai-client
~/.local/venvs/synai-client/bin/python -m pip install /path/to/SynAI
~/.local/venvs/synai-client/bin/synai-client --help
```

The dedicated environment and `synai-client` executable are independent of the
Core process. The Client Agent's module imports are limited to Python standard
library code, `cryptography`, `httpx`, and `websockets`; it does not import the
TUI or execution stack. The installation does not request root privileges or
change system configuration.

## Configure and pair

Configure an exact Core origin. HTTP/WS is accepted only for loopback
development; non-loopback connections require HTTPS/WSS and the platform's
normal validated TLS certificate chain:

```sh
synai-client setup --server https://synai.example
```

In the authenticated SynAI Devices page, create a pairing challenge and run
`synai-client pair` on the Linux computer. Enter the challenge ID and its
one-time secret at the hidden-secret prompt, then type `PAIR` to locally
confirm enrollment. The private Ed25519 key is generated on that computer and
never transmitted. The newly enrolled device is pending until an administrator
explicitly authorizes it in the Devices page. Challenges expire after five
minutes and can be used only once.

Client configuration and credentials live in
`~/.config/synai-client/`. The directory must be owned by the current user and
mode `0700`; JSON state and the unencrypted Ed25519 private key must be
user-owned mode `0600` regular files, opened without following symlinks.
Unsafe ownership, modes, file types, or path symlinks cause a hard error.
Back up this directory only to a protected user-owned location. Do not send
`identity.json` to anyone; it contains the private key and device credential.

The browser session cookie and CSRF token are never stored or used by the
Agent. The short-lived pairing challenge secret is separate from the device
credential and is not reusable.

## Locally authorize a workspace

Create a logical project and authorize a device in the browser. In its
Workbench, create a workspace binding and note the opaque binding ID. The
binding contains a safe alias but no local filesystem path. On the Linux
computer, explicitly associate that binding with a local directory:

```sh
synai-client workspace add \
  --project-id PROJECT_ID \
  --binding-id BINDING_ID \
  --alias "my checkout" \
  /home/me/src/example
synai-client workspace list
synai-client workspace remove --binding-id BINDING_ID
```

The canonical directory path and filesystem identity stay in the private
client registry. The Agent rejects symlink path components, verifies the root
identity on use, opens directories/files with no-follow flags, rejects special
files and traversal, and detects file replacement or changes during capture.
Removing a local binding immediately prevents another local snapshot. Core
also revalidates that the matching server-side binding is active before the
Agent scans and again before it begins upload.

Registration grants neither write access nor command execution. Only the
approved directory is scanned; no server-supplied path is opened.

## Preview and snapshot upload

Run the one-shot flow:

```sh
synai-client snapshot --binding-id BINDING_ID
```

The preview displays the project, local alias, destination Core, every
included relative path, excluded/rejected paths and reasons, file count, and
estimated bytes. After reviewing the immutable-snapshot privacy warning, type
`UPLOAD` to approve that exact capture. Any other response or end-of-input
denies the upload. A prior workspace authorization or a browser request never
grants upload consent.

The persistent `connect` command can receive a versioned, time-bounded
snapshot request from the authenticated Core. It validates the request's
project/binding against its local registry, then performs the same preview and
explicit local confirmation; a non-interactive process denies the request.
`status` performs a one-shot authenticated connection check.

The client implements the Phase 13C limits without truncation: 500 files,
1 MiB per file, 64 MiB total, and server-advertised chunks no larger than
256 KiB. It only transfers regular UTF-8 text files with a supported
extension. Common credential files and development/cache directories are
conservatively excluded. Heuristic filtering does not guarantee that secrets
are absent; inspect the preview. Local snapshots are held in memory while
transferring (bounded by the existing 64 MiB limit). A lost/uncertain transfer
is not automatically resumed or replayed after process restart; run a fresh
preview and approve a new operation.

The Core constructs and hashes the final canonical manifest; the client
checks the returned manifest digest and the device, project, and binding
identities. The manifest contains relative paths only. Committed snapshots
remain immutable.

## Connection and lifecycle commands

```sh
synai-client connect       # persistent outbound WebSocket with signed heartbeat
synai-client status        # one-shot connection/status check
synai-client diagnose      # bounded non-secret diagnostics
synai-client disconnect    # close/reclaim the device's active connection
synai-client disconnect --forget
```

The client uses versioned Ed25519-authenticated messages, per-connection
monotonic sequence numbers, unique nonces, and bounded expirations. It
reconnects with exponential backoff capped at 30 seconds. Server-side live
connection state is held in memory only; restart requires client
reauthentication. A second connection for the same device replaces the older
one. Credential expiry or revocation stops authenticated operations; the
administrator must revoke and pair a new device identity when re-enrollment
is required. `disconnect --forget` erases local credentials/approvals but does
not revoke the server record; revoke a compromised device in the Devices page
as well.

An optional **user-level** systemd unit can be created manually; it is not
installed or enabled by SynAI. For example, save the following as
`~/.config/systemd/user/synai-client.service`, adjust the executable path, and
explicitly run `systemctl --user daemon-reload` and `systemctl --user start
synai-client.service` if desired:

```ini
[Unit]
Description=SynAI Client Agent
After=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/venvs/synai-client/bin/synai-client connect
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
PrivateTmp=true

[Install]
WantedBy=default.target
```

Without an interactive terminal a service cannot approve snapshots; local
confirmation fails closed. Do not add an approval mechanism that bypasses the
preview or user consent.

## Troubleshooting and compatibility

- `Core connection failed`: confirm the configured origin, DNS/routing,
  reverse-proxy WebSocket upgrade support, and TLS certificate validity.
- `protocol_unsupported`: upgrade both Core and Client Agent; protocol version
  1 is the only currently supported version.
- `device_credential_invalid` or expiry: check administrator authorization,
  revocation, and credential expiry; re-pair through a new challenge.
- `binding_stale` or binding mismatch: ensure the server binding is active and
  matches the locally registered project/binding IDs.
- `snapshot_*` limit or file errors: inspect the rejected entries and reduce
  the selected file set; files are never truncated or silently omitted from a
  purported complete snapshot.
- Permission errors: restore owner-only `~/.config/synai-client/` and
  `0600` state-file permissions, and remove path symlinks.

Protocol messages use opaque IDs and POSIX-independent relative paths; local
canonical paths never enter the wire protocol. Windows and graphical desktop
clients are not implemented. WebSocket snapshot requests are transient and
expire after ten minutes. Status polling and browser activity are metadata,
not authority to execute or mutate project files.

## Phase 13E implementation boundaries

Snapshot content is sent only after local confirmation over the existing
authenticated device HTTP API, directly into Core's immutable snapshot store;
there is no client-to-Broker transfer. The Core independently authenticates
the device, validates project/binding association, reconstructs the manifest,
and verifies each uploaded chunk/file digest. The browser can request a
snapshot but cannot approve it locally or read source bytes.

The client implements no shell, command execution, patching, editing, Git
mutation, restore, process control, or arbitrary remote operation. The WSS
server accepts only hello, heartbeat/status/disconnect, and bounded
snapshot-operation acknowledgments; unknown messages close the connection.
The separate Phase 13F Broker and task execution remain disabled.
