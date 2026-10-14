# Native descriptor-relative SQLite VFS prototype

**Isolated executable prototype, not production acceptance or a sandbox.**
The current implementation supports tested rollback-journal and WAL operations,
including descriptor-relative SHM mapping and locking. It **supersedes the
earlier rollback-only, `iVersion=1` version**. Current file methods advertise
`iVersion=2` and implement all four `xShm*` callbacks. Readonly WAL, disk temporary
files, and multi-database super-journals remain unsupported.

No production Broker or registry adapter is changed. The acceptance matrix
records this prototype as custody-dependent offline evidence, not production
acceptance.

## Build and reproduce

From the repository root:

```sh
make -C prototypes/sqlite-dirfd-vfs test
```

Uses the installed C compiler, `make`, `pkg-config`, SQLite headers, and CPython
with `enable_load_extension` / `load_extension`. Nothing is installed. The
Makefile selects `/usr/bin/python3`; override with
`PYTHON=/path/to/extension-capable/python`. The environment's pyenv `python3`
lacks those Python methods and cannot run these tests.

Both extensions use `sqlite3ext.h`'s API dispatch table from the **loading
connection's engine**. They intentionally do not link `-lsqlite3`, which could
register a VFS in a different engine. API pointers are translation-unit-local
to avoid symbol collisions between independently loaded extensions.

The tests really load through CPython, close the loading connection, then open
other CPython connections using `vfs=dirfd_prototype`. The test-only second
extension resolves the VFS through that same engine and exercises its ABI.
No `ctypes` SQLite registration or connection-object layout tricks are used.

Tests use disposable directories **inside this prototype directory**, not
`/tmp`. No containers, privileged actions, kernel quota operations, production
databases, or production paths are used.

### Sanitizer and compiler checks

Using installed GCC and sanitizer runtimes:

```sh
make -C prototypes/sqlite-dirfd-vfs clean
make -C prototypes/sqlite-dirfd-vfs \
  CFLAGS='-O1 -g -std=c11 -Wall -Wextra -Werror -fPIC -fsanitize=address,undefined -fno-sanitize-recover=all' \
  LDFLAGS='-shared -Wl,-z,defs -fsanitize=address,undefined' all
ASAN_OPTIONS=detect_leaks=0:abort_on_error=1 \
LD_PRELOAD="$(cc -print-file-name=libasan.so)" \
  /usr/bin/python3 -m unittest discover -s prototypes/sqlite-dirfd-vfs -v
make -C prototypes/sqlite-dirfd-vfs clean
make -C prototypes/sqlite-dirfd-vfs test
```

LeakSanitizer is disabled: this run checks native address and undefined-behavior
errors, not whole-interpreter leak freedom. Permanent fd-count tests cover
descriptor cleanup. Both extension translation units are also compiled with
`-std=c11 -Wall -Wextra -Werror -fanalyzer -fPIC -c`, with output objects removed
afterward. This performs GCC analysis rather than only a syntax check.

Recorded normal-build and ASan/UBSan results: **19 tests passed each**, with no
final sanitizer reports, on GCC 15.2.0, system CPython
3.14.4, SQLite 3.46.1, and filesystem magic **0xef53**. The sanitizer run initially
found duplicate exported `sqlite3_api` globals; making each dispatch pointer
local corrected this. GCC's analyzer also completed without diagnostics.

## Retained directory-descriptor handoff

Python supplies a **borrowed integer dirfd**, not a pathname. The extension
duplicates it with `F_DUPFD_CLOEXEC`, validates it with `fstat`, and requires:

* a directory owned by the effective UID;
* **no group/world permission bits**, stricter than merely rejecting write bits;
* a usable directory descriptor, not `O_PATH`;
* Linux ext-family filesystem magic `0xef53` (`EXT4_SUPER_MAGIC`, also shared by
  ext2/ext3). Other filesystem types fail closed rather than being qualified
  by inference.

Ownership/private permissions are rechecked during descriptor-relative
filesystem operations. Permission loss makes existing and new operations fail;
unlock and close remain available to release retained authority. These checks
do **not** prevent a malicious same-UID process from changing permissions again.

The duplicate is immutable until process exit. Configuring the same inode is
idempotent; a different inode is rejected. The caller may close its original
fd after successful handoff. The extension requests `SQLITE_OK_LOAD_PERMANENTLY`
so registered callbacks cannot point into unloaded code. One root descriptor
and the extension mapping remain until process exit.

Example in a disposable process:

```python
import os
import sqlite3

# Caller owns path resolution and acquisition of its retained authority.
fd = os.open("/path/to/private-disposable-directory",
             os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
loader = sqlite3.connect(":memory:")
loader.enable_load_extension(True)
loader.load_extension("/absolute/path/to/dirfd_vfs.so")
loader.enable_load_extension(False)
try:
    loader.execute("select dirfd_vfs_root(?)", (fd,)).fetchone()
finally:
    os.close(fd)
loader.close()

db = sqlite3.connect("file:example.db?vfs=dirfd_prototype", uri=True)
assert db.execute("pragma journal_mode=wal").fetchone() == ("wal",)
db.execute("create table t(value)")
db.execute("insert into t values(42)")
db.commit()
db.close()
```

The VFS never opens a root pathname. Tests acquire a dirfd **before** renaming
and replacing its pathname, hand off that fd, close the caller's copy, and
verify database creation stays in the retained directory. Caller-side
`O_NOFOLLOW` protects the final path component, not ancestors; choosing how to
acquire the initial capability is the trusted caller's responsibility.

## File lifecycle and I/O

No default/pathname VFS wrapping, delegation, or default VFS replacement occurs.
Database, main journal, WAL, and SHM files all use
`openat(root_fd, leaf, ... O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK)`. Creation first uses
`O_CREAT|O_EXCL` and mode 0600, never truncating an existing database on open.
Lookup/deletion use `fstatat(..., AT_SYMLINK_NOFOLLOW)` and `unlinkat`.

Only flat ASCII leaf names up to 200 bytes are allowed. Absolute paths,
separators, traversal, leading dots, and reserved sidecar names as main database
names fail. Derived names must fit as well. Symlinks, directories, FIFOs,
and multiply-linked regular files fail. Nonblocking opens avoid waiting on a
FIFO before checking its type.

`pread`/`pwrite` handle interruption and partial transfers. Short reads zero-fill
and return `SQLITE_IOERR_SHORT_READ`. File size, truncation, full/data-only sync,
initial creation directory sync, and requested deletion directory sync are
implemented. The WAL index is volatile and does not require `msync` for durable
commits; WAL durability follows SQLite's WAL file sync protocol. No database
`xFetch`/mmap is offered; `mmap` is used only for SHM.

Readonly rollback databases work. Readonly WAL is intentionally rejected with
`SQLITE_IOERR_SHMOPEN`, rather than invoking a private heap-index fallback.
`immutable=1` and `nolock=1` are rejected. Unknown file-control operations return
`SQLITE_NOTFOUND`, the SQLite contract for an unimplemented control.

## OFD locking and WAL index

Each database connection has separate database and SHM open-file descriptions.
Linux `F_OFD_SETLK` / `F_OFD_GETLK` keep lock ownership separate even within one
process, and closing a peer descriptor cannot release another peer's locks.
No process-wide POSIX-lock fallback is used.

Database locks use SQLite's conventional pending/reserved/shared byte ranges.
Failed exclusive upgrades retain PENDING to block new readers. Unlock and
downgrade release authority even after a detected identity/custody failure.

SHM uses:

* `MAP_SHARED`, read/write mappings of **32768-byte** regions; system page size
  must divide that region size; at most **128 regions** (4 MiB of index mappings
  per connection) are supported. Other sizes/indices fail `SQLITE_IOERR_SHMMAP`.
* SQLite Unix SHM lock bytes **120–127**, shared or exclusive OFD locks, with
  a single kernel operation for exclusive ranges; invalid flags/ranges fail.
* Dead-man byte **128**, held shared for the entire mapping lifetime. An
  exclusive acquisition proves no other live holders before truncating a stale
  index; it is then downgraded to shared. Contention with initialization returns
  `SQLITE_BUSY`, never a fallback to unsafe mapping.
* Prototype-only byte **129** serializes growth: size is re-read under this lock
  before enlargement, preventing a smaller concurrent extension from shrinking
  an already enlarged index. Growth contention returns `SQLITE_BUSY`.
* A sequentially consistent C atomic fence for `xShmBarrier`.
* Unmap releases regions and SHM locks, attempts exclusive dead-man acquisition
  before requested deletion, verifies inode identity, then closes the descriptor.
  A remaining holder prevents deletion. Without delete, the index is retained
  on disk and reinitialized after all dead-man holders are gone.

The supported concurrent population is **this prototype VFS**. Compatibility
with a simultaneous ordinary SQLite Unix VFS opener is not qualified, even
though conventional lock offsets are used. Unnamed temp files, temp/transient
databases, temp/sub/super-journals, delete-on-close files, and VFS-driven extension
loading fail closed. Disk-spilling savepoints/sorts can therefore fail.

## Identity semantics and independent review reconciliation

Database, journal, and WAL reads, writes, truncation, synchronization, and size
queries all compare the retained inode with the descriptor-relative named inode
and require a single link. Database locking and SHM mapping/lock acquisition
check identities as well. Same-process live-file bookkeeping rejects joining a
different inode under an active name. SHM mapping likewise rejects a replacement
inode while another local holder is live.

Main-file records remember opened journal/WAL identities through their close
until deletion. Delete verifies those recorded identities and any still-live
same-name handles before unlinking; successful deletion clears the remembered
identity. SHM deletion uses its retained descriptor directly. Tests replace
DB/journal/WAL/SHM files between operations, exercise the error path and peer
open, then close without restoring the name first: replacement contents survive.

**The review's check-then-use concerns remain real:** `fstatat` plus I/O or
`unlinkat` is not an atomic identity-and-use operation. The checks detect the
tested sequential mutations, not hostile concurrent mutation. Private ownership
and mode are necessary but do not exclude malicious same-UID actors. If such an
actor is in scope, **this prototype is not a suitable authority boundary**.
Mapped index access also cannot report truncation errors safely; external
truncation of live SHM can cause `SIGBUS`. No race-proof or same-UID adversary
claim is made. Cross-process local bookkeeping cannot identify another
process's original inode after malicious namespace substitution.

## Permanent test coverage

`test_dirfd_vfs.py` covers:

* shared-engine registration after loader closure and CLOEXEC root retention;
* DELETE/TRUNCATE/PERSIST transactions, rollback, VACUUM shrink, reopen,
  integrity checks, cross-process locks, and hot-journal crash recovery;
* actual `journal_mode=WAL` returning **`wal`**, two-connection snapshot isolation,
  writer contention, partial passive checkpoint, busy FULL/RESTART/TRUNCATE
  checkpoints with a pinned reader, then successful checkpoint/reset/truncation;
* close of one peer while another writes; last-close WAL/SHM cleanup;
* cross-process WAL writer exclusion and committed/uncommitted crash recovery;
* four independent threaded writers, a pinned snapshot, and a real engine index
  extending beyond the first 32768-byte region;
* DB/journal/WAL/SHM replacement and preservation of foreign replacement files;
  preexisting symlink/hardlink/directory/FIFO rejection and subsequent safe reuse;
* readonly WAL rejection, invalid paths/options, invalid root arguments/fds,
  private-permission requirements and permission loss, immutable root, borrowed
  dirfd closure and root pathname substitution, unsupported extension loading,
  rejected super-journal commit with rollback, and fd counts after errors.

`test_probe.c` verifies the native ABI in the loading engine: short-read
zero filling, invalid offsets, truncation/size/full/data sync, database locks,
shared memory visibility across two descriptors and regions, shared/exclusive
range conflicts, invalid SHM requests, peer-close lock retention, no unlink with
live dead-man holders, stale-index reset after last holder, WAL open/sync/delete,
unsupported file classes, and failure `pMethods == NULL`.

## Remaining limitations

* Native caller-process code is not a sandbox. Trusted callers own SQL policy,
  initial capability acquisition, retained-dirfd lifetime during handoff, and
  connection creation. They can deliberately select another VFS or use ordinary
  filesystem APIs; SQL `synchronous=OFF` and `journal_mode=OFF/MEMORY` are not
  prohibited by this VFS.
* Directory mode/UID checks and sequential identity checks are not adversarial
  same-UID protection. Symlink/hardlink rejection does not repair concurrent
  namespace substitution or direct writes/truncation.
* Only the tested Linux ext-family environment is in scope. Non-ext-family
  roots fail configuration. An ext-family magic match alone is not a complete
  mount/hardware qualification. A conservative 4096-byte sector size is
  advertised without physical-sector discovery.
* Tests cover process-crash recovery, not physical power-loss durability,
  disk-full behavior, exhaustive syscall failure injection, or huge WALs at the
  128-region limit. EOF/mapping/lock failures release resources on close; a
  failed new SHM initialization can leave an orphan index file for safe
  reinitialization, rather than blindly unlinking a possibly shared inode.
* Readonly WAL, multi-database super-journals, disk temp/sub-journals, shared
  cache, fork with live inherited handles, concurrent extension initialization,
  and mixed VFS interoperability are not qualified. A different SQLite API table
  is rejected on extension reinitialization rather than overwriting its dispatch
  pointer. The initial engine's thread-safe configuration is assumed.

## Required authority boundary before registry use

No C-side check can stop an untrusted same-UID writer from mutating the
database inode, replacing a directory entry between check and `unlinkat`, or
truncating a live mapped SHM file. The smallest VFS-preserving target is to run
the one pinned CPython SQLite engine and this registered VFS inside an exclusive
registry-owner process whose non-login UID and private directory ancestry are
independently enforced. Broker, Core and task identities must not be able to
open registry files for write, rename/unlink sidecars, or truncate SHM.

That owner must keep the complete existing registry transaction and recovery
logic (including schema, replay protection, seals, WAL/checkpoint and durable
completion) and expose only bounded, authenticated domain operations over a
protected Unix socket. It must not expose SQL, SQLite connection/cursor objects,
or writable DB/sidecar descriptors. Socket peer credentials alone are not
sufficient if another untrusted process can share the Broker UID. The owner
identity, directory custody, Broker/task denial, and restart recovery must be
independently verified before any registry integration. If those conditions
cannot be enforced, stop and use a separately protected storage-owner service;
do not weaken the same-UID threat model or treat more inode checks as a fix.

No such owner, IPC service, service identity, or production adapter is installed
or implemented here. The prototype remains disconnected from the production
registry.

## Closeout findings relevant to acceptance (matrix unchanged)

Final focused rerun: `make -C prototypes/sqlite-dirfd-vfs test` passed all
**19 tests** in the previous implementation milestone; no VFS C code changed
in the current continuation. The implementation translation unit also passed
a fresh GCC `-fanalyzer` compilation without diagnostics. The previously
recorded full ASan/UBSan run passed the same 19 tests.

* **Executable evidence:** shared-engine registration, retained integer-dirfd
  handoff, descriptor-relative DB/journal/WAL/SHM lifecycle, OFD locking, SHM
  mappings, transactions, checkpoints, process-crash recovery, and sequential
  replacement rejection in the documented private ext-family environment.
* **Conditional assumptions:** the effective UID exclusively controls the
  directory and its files; permission checks enforce private mode but cannot
  prove exclusive same-UID custody. Concurrent users use this VFS, not a mixed
  pathname VFS population.
* **Unmet authority guarantee:** after checking a name's inode, a malicious
  same-UID writer can replace that directory entry before `openat`/`unlinkat`
  or directly modify/truncate a retained or mapped inode. In particular,
  `unlinkat` cannot condition deletion atomically on the previously checked
  inode, and a fresh process cannot infer an older process's original identity
  from a replaced name. Descriptor-relative lookup does not eliminate this
  namespace/authority-lifetime gap. External live SHM truncation can cause
  `SIGBUS`. Sequential replacement tests are not proof against these races.
* **Disposition:** retain this tested custody-dependent prototype as evidence,
  not production-grade hostile-mutation containment or acceptance of a
  race-free descriptor/name boundary. Production integration and privileged
  testing remain unperformed and gated.

Files: `dirfd_vfs.c`, `test_probe.c`, `test_dirfd_vfs.py`, `Makefile`, `.gitignore`,
and this `README.md`. Generated `.so` files/bytecode are ignored; `make clean`
removes them.
