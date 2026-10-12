# Offline Linux project-quota ABI contract

Phase 13F-A.5.1c.2 is a **library-only, dependency-free, pure transformation**
prototype. Both this crate and the [native validator](../quota-inspector-native/README.md)
forbid unsafe code. There is no FFI declaration, syscall wrapper, transport API,
feature, environment switch, executable, daemon, listener or production wiring.
The native validator does not depend on this crate and still returns unavailable.
No successful decode is a storage observation or authorization.

## Target and local authoritative headers

Only `x86_64-unknown-linux-gnu`, little-endian LP64 is supported. Other targets
fail compilation explicitly; no architecture-independent syscall number is assumed.
Reviewed installed headers (Ubuntu packages, no downloads):

- `linux-libc-dev:amd64 7.0.0-38.38`: `/usr/include/linux/quota.h`,
  `/usr/include/x86_64-linux-gnu/asm/unistd_64.h`.
- `libc6-dev:amd64 2.43-2ubuntu2.4`:
  `/usr/include/x86_64-linux-gnu/sys/quota.h`, `errno.h` and target errno headers.
- `linux-headers-7.0.0-38 7.0.0-38.38`:
  `/usr/src/linux-headers-7.0.0-38/include/linux/syscalls.h`,
  `include/linux/quota.h`, `include/linux/projid.h`.

The kernel declaration has a signed-long result and arguments `unsigned int fd`,
`unsigned int cmd`, `qid_t id`, `void __user *addr`; `qid_t` is `__kernel_uid32_t`.
The x86_64 syscall number is **443**. This is a declaration/ABI identity, not
runtime support. The installed GNU libc quota header declares legacy `quotactl`,
not `quotactl_fd`; this phase does not fabricate a libc declaration or wrapper.
GETINFO's ID is fixed to zero (no per-project record is selected).
Actual kernel implementation behavior and user-namespace ID mapping are not
qualified by these headers or compile-only checks.

## Narrow command and buffer contract

| Typed command | Unsigned encoding | ID | Output |
|---|---|---|---|
| `GetQuota(ProjectId)` | `0x80000702` | syntactic project ID | 72-byte if_dqblk |
| `GetInfo` | `0x80000502` | fixed zero | 24-byte if_dqinfo |

Encoding uses checked unsigned multiplication by 256 and project quota type 2,
never a signed shift or truncating cast. `ProjectId` accepts 1 through
`u32::MAX - 1`: zero is deliberately excluded by this prototype's positive-project
contract, and all-ones is the kernel invalid-project sentinel. Representability
does not establish namespace mapping or approved inventory assignment.
No raw-command constructor/transport exists. Setters, quota on/off, sync,
enumeration, GETFMT, ioctl, legacy quotactl, paths/devices and shell fallback are
unrepresentable as commands in this API.

The `repr(C)` records have private fields, safe initialized defaults and fixed,
zero-filled byte buffers. No byte casting or uninitialized memory is used.
All layout sizes, alignments and field offsets have Rust constant assertions.
The compile-only [C check](tests/abi-layout.c) independently asserts installed
header widths/offsets, commands, masks, unit size, syscall number and errno values.
It has no function and is compiled only with `-fsyntax-only`.

| Structure | Fields / offsets | Size / alignment |
|---|---|---|
| if_dqblk | eight u64 at 0,8,16,24,32,40,48,56; u32 validity at 64 | 72 / 8 |
| if_dqinfo | u64 grace at 0,8; u32 flags at 16; u32 validity at 20 | 24 / 8 |

QuotaRecord explicitly initializes its four trailing reserved bytes; incoming
kernel tail padding is ignored, never interpreted as data. Byte parsing requires
the exact buffer size and little-endian fields; it does not read a pointer.

## Decoded candidates remain untrusted

Quota decoding requires BLIMITS, SPACE, ILIMITS and INODES (`0x0f`).
Unknown bits outside `0x3f` are rejected. Optional BTIME/ITIME produce `None`
when absent, not a fabricated timestamp. Valid BTIME/ITIME values are Unix
expiry timestamps; GETINFO grace values are durations in seconds.
Hard/soft space limits use **1024-byte quota units**, independent of filesystem
block size; current space is already bytes. Inode values are counts. Multiplication
overflow is rejected. Zero limits are explicitly `Unlimited`, never positive caps.
There is no special all-ones space-limit sentinel: that value overflows the byte
conversion and is rejected. All-ones inode counts remain untrusted u64 values.

Soft limits exceeding a nonzero hard limit are refused as contradictory candidates.
Usage above a hard limit is preserved, not used to assert enforcement: changes
to limits and quota semantics require independent validation. `positive_hard_caps`
checks representation only, not policy ceilings or current usage. Its successful
Rust Result does not confer security authority.

Info decoding requires all three validity bits (`7`), rejects unknown validity
and flags, and exposes only root-squash/system-file metadata (`0x10001`).
Missing configuration is an explicit error, not a zero/default success.
Neither these flags nor successful GETQUOTA establish accounting/enforcement state.
No candidate establishes UUID, mount/namespace identity, descriptor qualification,
trusted inventory, project assignment/inheritance, existing-file charging,
active write denial, a validated observation or Broker readiness.

Positive errno is classified without retries or fallback. EINVAL is deliberately
ambiguous (command/ID/arguments), not falsely attributed to one cause. ENOSYS,
EOPNOTSUPP/ENOTSUP, EPERM/EACCES, EBADF, missing-record errors, EINTR, EFAULT,
I/O, cancellation and timeout have explicit classes. Unknown values are preserved,
including invalid zero/negative inputs; they do not become success.

## Future descriptor and physical-completion contract

There is **no production adapter** in this crate. A future separately approved
adapter must accept only an already-approved owned filesystem descriptor, never
a path or block device. Descriptor kind, flags, identity and namespace/project
mapping must be qualified independently before and after acquisition.
The existing native validator's sealed inventory-bound adapter contract remains
the authority boundary; candidates cannot construct its private successful result.

Authority outcome, physical work and descriptor ownership must stay separate:

| Existing request state | Adapter result | Authority / resources |
|---|---|---|
| Live before deadline | Pending | unresolved; retain worker and owner leases |
| Live before deadline | Complete | decode untrusted candidate; release only after confirmed completion |
| Cancelled / expired | Pending | first failure stays sticky; work unresolved; retain leases |
| Cancelled / expired | Complete(Ok/Err) | first failure stays sticky; release after safe completion/cleanup |
| Owner dropped, work unresolved | no completion | independent worker lease retained; admission remains busy |
| Safely finalized old request | new request | independent state; no inherited local terminal failure |

Cancellation/timeout is not confirmation that a syscall stopped. A borrowed FD
must not outlive its retained owned lease. Completion cannot be asserted while
any physical query still uses it or cleanup remains unresolved. No detached
worker, unbounded queue or retry loop is provided. Potentially blocked native
operations will require an independently supervised process with verified
termination/reaping and ownership recovery; cooperative clocks or RAII alone do
not establish that. This supervision is not implemented in this phase.

## Deterministic tests and limits of qualification

Test-only fake transport/operation models exercise typed outputs, errno, pending
work, bounded repeated polling, first-terminal-failure retention, late completion,
lease retention/release and fresh operations. They compile only under `cfg(test)`;
they are contract simulations, not a second production lifecycle implementation.
The unchanged native tests separately verify the **actual** corrected observation
state machine, busy admission, cancellation, completion and dropped worker leases.
An inert owned FD used in ownership tests is never sent to a quota query.
Malformed buffers, all mask combinations, optional times, zero/unlimited,
soft/hard contradictions and conversion boundaries have permanent tests.
Compile-fail documentation tests exclude raw/mutating commands, public fakes,
unchecked IDs and real/legacy/ioctl invokers.

Run offline with the installed toolchain:

```sh
cargo test --locked --offline
cargo build --locked --offline --release
cargo fmt --check
cargo clippy --locked --offline --all-targets -- -D warnings
```

Kernel implementation/source semantics, FD suitability, user-namespace mappings,
actual returned masks, filesystem support, privileges, zero/unlimited behavior,
enforcement/write-denial, blocked-call supervision and post-crash cleanup still
require separate qualification. A separately approved disposable VM plan must
also pass trusted inventory, filesystem/bind-source, native SQLite VFS, storage,
runtime and containment acceptance. No quota syscall, privileged setup, container
or quota-lab operation is authorized by this offline crate.
Entitlements, reserve, operational allowance and the **128 GiB** maximum future
storage allocation are unchanged. **Broker execution remains unavailable.**
