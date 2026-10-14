"""Real CPython SQLite connections; all disposable directories stay here."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
EXTENSION = HERE / "dirfd_vfs.so"
VFS = "dirfd_prototype"


def load(root):
    loader = sqlite3.connect(":memory:")
    loader.enable_load_extension(True)
    loader.load_extension(str(EXTENSION))
    loader.enable_load_extension(False)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        loader.execute("select dirfd_vfs_root(?)", (fd,)).fetchone()
    finally:
        os.close(fd)
    return loader


def connect(name, extra=""):
    return sqlite3.connect(
        f"file:{quote(name, safe='')}?vfs={VFS}{extra}",
        uri=True, timeout=0, isolation_level=None,
    )


def descriptor_count():
    return len(os.listdir("/proc/self/fd"))


class DirfdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.disposable = tempfile.TemporaryDirectory(prefix="test-data-", dir=HERE)
        cls.root = Path(cls.disposable.name)
        try:
            cls.loader = load(cls.root)
        except Exception:
            cls.disposable.cleanup()
            raise
        # Closing the loading connection must not unload registered callbacks.
        cls.loader.close()

    @classmethod
    def tearDownClass(cls):
        cls.disposable.cleanup()

    def db(self, name):
        return contextlib.closing(connect(name))

    def test_shared_engine_registration_and_descriptor_root(self):
        name = "registration.db"
        before = descriptor_count()
        with self.db(name) as db:
            db.execute("create table t(x)")
            db.execute("insert into t values(42)")
            self.assertEqual(db.execute("select x from t").fetchone(), (42,))
            self.assertEqual(db.execute("pragma integrity_check").fetchone(), ("ok",))
            targets = []
            for fd in os.listdir("/proc/self/fd"):
                try:
                    targets.append(os.readlink(f"/proc/self/fd/{fd}"))
                except FileNotFoundError:
                    pass
            self.assertIn(str(self.root / name), targets)
        self.assertEqual(descriptor_count(), before)
        self.assertTrue((self.root / name).exists())
        self.assertFalse((HERE / name).exists())
        pinned = []
        for fd_text in os.listdir("/proc/self/fd"):
            try:
                if os.readlink(f"/proc/self/fd/{fd_text}") == str(self.root):
                    pinned.append(int(fd_text))
            except FileNotFoundError:
                pass
        self.assertEqual(len(pinned), 1)
        self.assertTrue(fcntl.fcntl(pinned[0], fcntl.F_GETFD) & fcntl.FD_CLOEXEC)

    def test_native_vfs_abi(self):
        before = descriptor_count()
        with contextlib.closing(sqlite3.connect(":memory:")) as loader:
            loader.enable_load_extension(True)
            loader.load_extension(str(HERE / "test_probe.so"))
            loader.enable_load_extension(False)
            self.assertEqual(
                loader.execute("select dirfd_vfs_native_probe()").fetchone(),
                ("rollback and WAL/SHM ABI OK",),
            )
        self.assertEqual(descriptor_count(), before)
        self.assertFalse((self.root / "native.db").exists())
        self.assertFalse((self.root / "unsupported").exists())
        self.assertFalse((self.root / "delete-on-close.db").exists())

    def test_transactions_journal_modes_truncation_and_reopen(self):
        for mode in ("delete", "truncate", "persist"):
            name = f"transactions-{mode}.db"
            with self.db(name) as db:
                self.assertEqual(db.execute(f"pragma journal_mode={mode}").fetchone(), (mode,))
                db.execute("pragma synchronous=full")
                db.execute("create table t(x blob)")
                db.execute("begin immediate")
                db.executemany("insert into t values(?)", [(b"x" * 4096,)] * 64)
                self.assertTrue((self.root / f"{name}-journal").exists())
                db.execute("rollback")
                self.assertEqual(db.execute("select count(*) from t").fetchone(), (0,))
                db.execute("begin immediate")
                db.executemany("insert into t values(?)", [(b"y" * 4096,)] * 64)
                db.execute("commit")
                size = (self.root / name).stat().st_size
                db.execute("delete from t")
                db.execute("vacuum")
                self.assertLess((self.root / name).stat().st_size, size)
                self.assertEqual(db.execute("pragma integrity_check").fetchone(), ("ok",))
            with self.db(name) as db:
                self.assertEqual(db.execute("select count(*) from t").fetchone(), (0,))

    def test_multiple_connections_writer_reader_pending_locks(self):
        with self.db("locks.db") as first, self.db("locks.db") as second:
            first.execute("create table t(x)")
            first.execute("insert into t values(1)")
            second.execute("begin")
            self.assertEqual(second.execute("select * from t").fetchall(), [(1,)])
            first.execute("begin immediate")
            first.execute("insert into t values(2)")
            with self.assertRaises(sqlite3.OperationalError) as error:
                first.execute("commit")
            self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            with self.db("locks.db") as third:
                with self.assertRaises(sqlite3.OperationalError) as error:
                    third.execute("select * from t").fetchall()
                self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            second.execute("rollback")
            first.execute("commit")
            self.assertEqual(second.execute("select * from t").fetchall(), [(1,), (2,)])
            first.execute("begin immediate")
            with self.assertRaises(sqlite3.OperationalError) as error:
                second.execute("begin immediate")
            self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            first.execute("rollback")
            second.execute("begin immediate")
            second.execute("rollback")

    def test_cross_process_locking_and_hot_journal_recovery(self):
        name = "crash.db"
        with self.db(name) as db:
            db.execute("create table t(x blob)")
            db.execute("insert into t values(?)", (b"original",))
            db.execute("begin immediate")
            script = """
import os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_dirfd_vfs import load, connect
loader = load(Path(sys.argv[2]))
db = connect(sys.argv[3])
try:
    db.execute("begin immediate")
except sqlite3.OperationalError as error:
    assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY, error
else:
    raise AssertionError("cross-process writer did not block")
db.close()
loader.close()
"""
            subprocess.run(
                [sys.executable, "-c", script, str(HERE), str(self.root), name],
                check=True, timeout=20,
            )
            db.execute("rollback")
        script = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_dirfd_vfs import load, connect
loader = load(Path(sys.argv[2]))
db = connect(sys.argv[3])
db.execute("pragma cache_size=4")
db.execute("begin immediate")
db.execute("update t set x=?", (b"changed" * 20000,))
db.executemany("insert into t values(?)", [(b"uncommitted" * 2000,)] * 50)
os._exit(0)
"""
        subprocess.run(
            [sys.executable, "-c", script, str(HERE), str(self.root), name],
            check=True, timeout=20,
        )
        journal = self.root / f"{name}-journal"
        self.assertTrue(journal.exists())
        self.assertNotEqual(journal.read_bytes()[:8], b"\0" * 8)
        with self.db(name) as db:
            self.assertEqual(db.execute("select x from t").fetchall(), [(b"original",)])
            self.assertEqual(db.execute("pragma integrity_check").fetchone(), ("ok",))
        self.assertFalse(journal.exists())

    def test_wal_two_connections_and_all_checkpoints(self):
        name = "wal.db"
        before = descriptor_count()
        with self.db(name) as writer, self.db(name) as reader:
            self.assertEqual(writer.execute("pragma journal_mode=wal").fetchone(), ("wal",))
            writer.execute("pragma wal_autocheckpoint=0")
            writer.execute("pragma synchronous=full")
            writer.execute("create table t(x)")
            writer.execute("insert into t values(1)")
            reader.execute("begin")
            self.assertEqual(reader.execute("select * from t").fetchall(), [(1,)])
            writer.execute("insert into t values(2)")
            self.assertEqual(reader.execute("select * from t").fetchall(), [(1,)])
            passive = writer.execute("pragma wal_checkpoint(passive)").fetchone()
            self.assertEqual(passive[0], 0)
            self.assertGreater(passive[1], passive[2])
            for kind in ("full", "restart", "truncate"):
                busy = writer.execute(f"pragma wal_checkpoint({kind})").fetchone()
                self.assertEqual(busy[0], 1)
                self.assertGreater(busy[1], busy[2])
            reader.execute("rollback")
            self.assertEqual(reader.execute("select * from t").fetchall(), [(1,), (2,)])
            writer.execute("begin immediate")
            writer.execute("insert into t values(3)")
            with self.assertRaises(sqlite3.OperationalError) as error:
                reader.execute("begin immediate")
            self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            writer.execute("rollback")
            for kind in ("passive", "full", "restart"):
                writer.execute("insert into t values(4)")
                result = writer.execute(f"pragma wal_checkpoint({kind})").fetchone()
                self.assertEqual(result[0], 0)
                self.assertGreater(result[1], 0)
                self.assertEqual(result[1], result[2])
            self.assertEqual(writer.execute("pragma wal_checkpoint(truncate)").fetchone(), (0, 0, 0))
            self.assertEqual((self.root / f"{name}-wal").stat().st_size, 0)
            self.assertTrue((self.root / f"{name}-shm").exists())
            reader.close()
            writer.execute("insert into t values(5)")
            self.assertEqual(writer.execute("pragma integrity_check").fetchone(), ("ok",))
        self.assertEqual(descriptor_count(), before)
        self.assertFalse((self.root / f"{name}-wal").exists())
        self.assertFalse((self.root / f"{name}-shm").exists())
        with self.db(name) as db:
            self.assertEqual(db.execute("select count(*) from t").fetchone(), (6,))

    def test_wal_cross_process_contention_and_crash_recovery(self):
        name = "wal-crash.db"
        script = """
import os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_dirfd_vfs import load, connect
loader = load(Path(sys.argv[2]))
db = connect(sys.argv[3])
if sys.argv[4] == "busy":
    assert db.execute("select * from t").fetchall() == [(1,)]
    try:
        db.execute("begin immediate")
    except sqlite3.OperationalError as error:
        assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
    else:
        raise AssertionError("WAL writer did not block")
    db.close()
else:
    db.execute("pragma wal_autocheckpoint=0")
    db.execute("insert into t values(2)")
    db.execute("begin immediate")
    db.execute("insert into t values(99)")
    os._exit(0)
"""
        with self.db(name) as writer:
            self.assertEqual(writer.execute("pragma journal_mode=wal").fetchone(), ("wal",))
            writer.execute("create table t(x)")
            writer.execute("insert into t values(1)")
            writer.execute("begin immediate")
            writer.execute("insert into t values(99)")
            subprocess.run(
                [sys.executable, "-c", script, str(HERE), str(self.root), name, "busy"],
                check=True, timeout=20,
            )
            writer.execute("rollback")
        subprocess.run(
            [sys.executable, "-c", script, str(HERE), str(self.root), name, "crash"],
            check=True, timeout=20,
        )
        self.assertTrue((self.root / f"{name}-wal").exists())
        self.assertTrue((self.root / f"{name}-shm").exists())
        with self.db(name) as db:
            self.assertEqual(db.execute("select * from t").fetchall(), [(1,), (2,)])
            self.assertEqual(db.execute("pragma wal_checkpoint(truncate)").fetchone(), (0, 0, 0))
            self.assertEqual(db.execute("pragma integrity_check").fetchone(), ("ok",))

    def test_wal_concurrent_threads_and_multiple_index_regions(self):
        name = "wal-regions.db"
        before = descriptor_count()
        with self.db(name) as observer:
            observer.execute("pragma journal_mode=wal")
            observer.execute("pragma wal_autocheckpoint=0")
            observer.execute("create table t(worker, value blob)")
            observer.execute("begin immediate")
            observer.executemany(
                "insert into t values(-1, ?)", [(b"x" * 16384,)] * 1100,
            )
            observer.execute("commit")
            self.assertGreaterEqual((self.root / f"{name}-shm").stat().st_size, 65536)
            # Keep a snapshot pinned while independent connections contend for writer locks.
            observer.execute("begin")
            self.assertEqual(observer.execute("select count(*) from t").fetchone(), (1100,))

            def write(worker):
                with self.db(name) as db:
                    db.execute("pragma busy_timeout=5000")
                    db.execute("pragma wal_autocheckpoint=0")
                    for value in range(25):
                        db.execute("begin immediate")
                        db.execute("insert into t values(?, ?)", (worker, str(value)))
                        db.execute("commit")
                    return db.execute("pragma integrity_check").fetchone()

            with ThreadPoolExecutor(max_workers=4) as workers:
                self.assertEqual(list(workers.map(write, range(4))), [("ok",)] * 4)
            self.assertEqual(observer.execute("select count(*) from t").fetchone(), (1100,))
            observer.execute("rollback")
            self.assertEqual(observer.execute("select count(*) from t").fetchone(), (1200,))
            self.assertEqual(
                observer.execute("select worker, count(*) from t where worker>=0 group by worker").fetchall(),
                [(0, 25), (1, 25), (2, 25), (3, 25)],
            )
            self.assertEqual(observer.execute("pragma wal_checkpoint(truncate)").fetchone(), (0, 0, 0))
        self.assertEqual(descriptor_count(), before)

    def test_wal_sidecar_symlink_hardlink_special_files_and_cleanup(self):
        outside = self.root / "wal-untouched"
        outside.write_bytes(b"unchanged")
        before = descriptor_count()
        for suffix in ("-wal", "-shm"):
            for kind in ("symlink", "hardlink", "directory", "fifo"):
                name = f"bad-{kind}{suffix}.db"
                sidecar = self.root / f"{name}{suffix}"
                with self.db(name) as db:
                    db.execute("create table t(x)")
                    if kind == "symlink":
                        sidecar.symlink_to(outside)
                    elif kind == "hardlink":
                        os.link(outside, sidecar)
                    elif kind == "directory":
                        sidecar.mkdir()
                    else:
                        os.mkfifo(sidecar)
                    with self.assertRaises(sqlite3.DatabaseError):
                        db.execute("pragma journal_mode=wal").fetchone()
                        db.execute("insert into t values(1)")
                    if db.in_transaction:
                        db.rollback()
                if kind == "directory":
                    sidecar.rmdir()
                else:
                    sidecar.unlink()
                with self.db(name) as db:
                    self.assertEqual(db.execute("pragma journal_mode=wal").fetchone(), ("wal",))
                    db.execute("insert into t values(2)")
                    self.assertEqual(db.execute("select * from t").fetchall(), [(2,)])
                    self.assertEqual(db.execute("pragma integrity_check").fetchone(), ("ok",))
        self.assertEqual(outside.read_bytes(), b"unchanged")
        self.assertEqual(descriptor_count(), before)

    def test_live_database_wal_and_shm_replacement_is_detected(self):
        before = descriptor_count()
        for suffix in ("", "-wal", "-shm"):
            name = f"replaced{suffix}.db"
            with self.db(name) as db:
                db.execute("pragma journal_mode=wal")
                db.execute("create table t(x)")
                db.execute("insert into t values(1)")
                path = self.root / f"{name}{suffix}"
                held = self.root / f"held{suffix}.db"
                path.rename(held)
                path.write_bytes(b"replacement-must-survive")
                try:
                    with self.assertRaises(sqlite3.DatabaseError):
                        db.execute("insert into t values(2)")
                    with self.assertRaises(sqlite3.DatabaseError):
                        with self.db(name) as peer:
                            peer.execute("select * from t").fetchall()
                finally:
                    if db.in_transaction:
                        db.rollback()
                    # Even closing an errored connection must not unlink the replacement.
                    db.close()
                    self.assertEqual(path.read_bytes(), b"replacement-must-survive")
                    path.unlink()
                    held.rename(path)
            with self.db(name) as db:
                self.assertEqual(db.execute("select * from t").fetchall(), [(1,)])
        self.assertEqual(descriptor_count(), before)

    def test_live_rollback_journal_replacement_fails_without_deleting_foreign_file(self):
        name = "replaced-journal.db"
        before = descriptor_count()
        with self.db(name) as db:
            db.execute("create table t(x)")
            db.execute("insert into t values(1)")
            db.execute("begin immediate")
            db.execute("insert into t values(2)")
            journal = self.root / f"{name}-journal"
            held = self.root / "held-journal"
            journal.rename(held)
            journal.write_bytes(b"replacement-must-survive")
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    db.execute("commit")
            finally:
                db.close()
                self.assertEqual(journal.read_bytes(), b"replacement-must-survive")
                journal.unlink()
                held.rename(journal)
        with self.db(name) as db:
            self.assertEqual(db.execute("select * from t").fetchall(), [(1,)])
            self.assertEqual(db.execute("pragma integrity_check").fetchone(), ("ok",))
        self.assertEqual(descriptor_count(), before)

    def test_readonly_wal_is_explicitly_unsupported(self):
        name = "readonly-wal.db"
        with self.db(name) as writer:
            writer.execute("pragma journal_mode=wal")
            writer.execute("create table t(x)")
            writer.execute("insert into t values(1)")
            with contextlib.closing(connect(name, "&mode=ro")) as reader:
                with self.assertRaises(sqlite3.DatabaseError) as error:
                    reader.execute("select * from t").fetchall()
                self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_IOERR_SHMOPEN)

    def test_root_permission_loss_fails_existing_and_new_connections_closed(self):
        before = descriptor_count()
        with self.db("custody.db") as db:
            db.execute("pragma journal_mode=wal")
            db.execute("create table t(x)")
            db.execute("insert into t values(1)")
            os.chmod(self.root, 0o720)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    db.execute("insert into t values(2)")
                with self.assertRaises(sqlite3.DatabaseError):
                    connect("custody-other.db").close()
            finally:
                os.chmod(self.root, 0o700)
            self.assertEqual(db.execute("select * from t").fetchall(), [(1,)])
        self.assertFalse((self.root / "custody-other.db").exists())
        self.assertEqual(descriptor_count(), before)

    def test_multi_database_super_journal_is_rejected(self):
        with self.db("multi-main.db") as db:
            db.execute("attach database 'multi-attached.db' as other")
            db.execute("create table main.t(x)")
            db.execute("create table other.t(x)")
            db.execute("begin immediate")
            db.execute("insert into main.t values(1)")
            db.execute("insert into other.t values(2)")
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("commit")
            if db.in_transaction:
                db.execute("rollback")
            self.assertEqual(db.execute("select * from main.t").fetchall(), [])
            self.assertEqual(db.execute("select * from other.t").fetchall(), [])
            self.assertEqual(db.execute("pragma main.integrity_check").fetchone(), ("ok",))
            self.assertEqual(db.execute("pragma other.integrity_check").fetchone(), ("ok",))

    def test_paths_and_unsafe_uri_options_rejected_without_leaks(self):
        names = (
            "../escape.db", "sub/db", "/absolute.db", ".", "..", ".hidden.db",
            "a\\b.db", "a" * 201, "test-wal", "test-shm", "test-journal",
        )
        before = descriptor_count()
        for name in names:
            with self.subTest(name=name), self.assertRaises(sqlite3.DatabaseError):
                connect(name).close()
        for option in ("&immutable=1", "&nolock=1"):
            with self.subTest(option=option), self.assertRaises(sqlite3.DatabaseError):
                connect("unsafe.db", option).close()
        self.assertEqual(descriptor_count(), before)
        self.assertFalse((self.root / "unsafe.db").exists())

    def test_symlinks_hardlinks_special_files_and_journal_failure_cleanup(self):
        outside = self.root / "untouched"
        outside.write_bytes(b"unchanged")
        os.symlink(outside, self.root / "symlink.db")
        os.link(outside, self.root / "hardlink.db")
        os.mkdir(self.root / "directory.db")
        os.mkfifo(self.root / "fifo.db")
        before = descriptor_count()
        for _ in range(10):
            for name in ("symlink.db", "hardlink.db", "directory.db", "fifo.db"):
                with self.subTest(name=name), self.assertRaises(sqlite3.DatabaseError):
                    connect(name).close()
        self.assertEqual(descriptor_count(), before)
        self.assertEqual(outside.read_bytes(), b"unchanged")
        name = "bad-journal.db"
        with self.db(name) as db:
            db.execute("create table t(x)")
            os.symlink(outside, self.root / f"{name}-journal")
            for _ in range(10):
                with self.assertRaises(sqlite3.DatabaseError):
                    db.execute("insert into t values(1)")
                if db.in_transaction:
                    db.rollback()
            (self.root / f"{name}-journal").unlink()
            db.execute("insert into t values(2)")
            self.assertEqual(db.execute("select * from t").fetchall(), [(2,)])
        self.assertEqual(descriptor_count(), before)
        self.assertEqual(outside.read_bytes(), b"unchanged")

    def test_readonly_and_root_immutability(self):
        with self.db("readonly.db") as db:
            db.execute("create table t(x)")
        with contextlib.closing(connect("readonly.db", "&mode=ro")) as db:
            self.assertEqual(db.execute("select * from t").fetchall(), [])
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("insert into t values(1)")
        loader = load(self.root)
        before = descriptor_count()
        with tempfile.TemporaryDirectory(prefix="other-root-", dir=HERE) as other:
            other_fd = os.open(other, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    loader.execute("select dirfd_vfs_root(?)", (other_fd,)).fetchone()
            finally:
                os.close(other_fd)
            for invalid in (other, None, 1.5, -1, 2**40):
                with self.subTest(invalid=invalid), self.assertRaises(sqlite3.OperationalError):
                    loader.execute("select dirfd_vfs_root(?)", (invalid,)).fetchone()
            for mode in (0o720, 0o702, 0o777, 0o755):
                os.chmod(other, mode)
                fd = os.open(other, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    with self.assertRaises(sqlite3.OperationalError) as error:
                        loader.execute("select dirfd_vfs_root(?)", (fd,)).fetchone()
                    self.assertIn("no group/world permissions", str(error.exception))
                finally:
                    os.close(fd)
            os.chmod(other, 0o700)
            for flags in (os.O_RDONLY, os.O_PATH):
                target = self.root / "readonly.db" if flags == os.O_RDONLY else other
                fd = os.open(target, flags | os.O_CLOEXEC)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        loader.execute("select dirfd_vfs_root(?)", (fd,)).fetchone()
                finally:
                    os.close(fd)
            closed = os.open(other, os.O_RDONLY | os.O_DIRECTORY)
            os.close(closed)
            with self.assertRaises(sqlite3.OperationalError):
                loader.execute("select dirfd_vfs_root(?)", (closed,)).fetchone()
        self.assertEqual(descriptor_count(), before)
        loader.close()

    def test_root_pin_survives_path_replacement(self):
        # This needs its own process because the root capability cannot be reset.
        script = """
import os, sqlite3, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_dirfd_vfs import load, connect
with tempfile.TemporaryDirectory(prefix="pin-", dir=sys.argv[1]) as disposable:
    base = Path(disposable)
    registration = sqlite3.connect(":memory:")
    registration.enable_load_extension(True)
    registration.load_extension(str(Path(sys.argv[1]) / "dirfd_vfs.so"))
    try:
        connect("unconfigured.db")
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError("unconfigured VFS opened a file")
    registration.close()
    named = base / "root"
    named.mkdir(mode=0o700)
    alias = base / "alias"
    alias.symlink_to(named, target_is_directory=True)
    registration = sqlite3.connect(":memory:")
    registration.enable_load_extension(True)
    registration.load_extension(str(Path(sys.argv[1]) / "dirfd_vfs.so"))
    try:
        registration.execute("select dirfd_vfs_root(?)", (str(alias),)).fetchone()
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("symlink root was accepted")
    registration.close()
    # Acquire authority before replacing its pathname, then hand off only the fd.
    fd = os.open(named, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    named.rename(base / "held")
    named.mkdir(mode=0o700)
    loader = sqlite3.connect(":memory:")
    loader.enable_load_extension(True)
    loader.load_extension(str(Path(sys.argv[1]) / "dirfd_vfs.so"))
    loader.execute("select dirfd_vfs_root(?)", (fd,)).fetchone()
    os.close(fd)
    db = connect("pinned.db")
    db.execute("create table t(x)")
    db.close()
    loader.close()
    assert (base / "held" / "pinned.db").exists()
    assert not (named / "pinned.db").exists()
"""
        subprocess.run([sys.executable, "-c", script, str(HERE)], check=True, timeout=20)

    def test_sql_extension_loading_is_rejected(self):
        with self.db("loading.db") as db:
            db.enable_load_extension(True)
            with self.assertRaises(sqlite3.OperationalError) as error:
                db.load_extension(str(EXTENSION))
            self.assertIn("disabled", str(error.exception))


if __name__ == "__main__":
    unittest.main()
