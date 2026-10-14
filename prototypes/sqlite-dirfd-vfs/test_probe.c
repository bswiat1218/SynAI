/* A separate test-only extension invokes the VFS ABI in the loading engine. */
#include <sqlite3ext.h>
static SQLITE_EXTENSION_INIT1
#include <string.h>

#define REQUIRE(condition) do { \
    if (!(condition)) { failure = #condition; goto cleanup; } \
} while (0)

static void probe(sqlite3_context *context, int argc, sqlite3_value **argv) {
    (void)argc;
    (void)argv;
    const char *failure = NULL;
    sqlite3_vfs *vfs = sqlite3_vfs_find("dirfd_prototype");
    sqlite3_file *first = NULL, *second = NULL;
    int flags = SQLITE_OPEN_MAIN_DB | SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE;
    int out = 0, reserved = 0, result = -1;
    sqlite3_int64 size = -1;
    unsigned char buffer[16];
    char pathname[8];
    void volatile *map1 = NULL, *map2 = NULL, *other_region = NULL;
    REQUIRE(vfs != NULL);
    first = sqlite3_malloc(vfs->szOsFile);
    second = sqlite3_malloc(vfs->szOsFile);
    if (first)
        memset(first, 0, (size_t)vfs->szOsFile);
    if (second)
        memset(second, 0, (size_t)vfs->szOsFile);
    REQUIRE(first && second);
    REQUIRE(vfs->xOpen(vfs, "native.db", first, flags, &out) == SQLITE_OK);
    REQUIRE(out == flags);
    REQUIRE(first->pMethods->iVersion == 2);
    REQUIRE(first->pMethods->xShmMap != NULL);
    REQUIRE(first->pMethods->xShmLock != NULL);
    REQUIRE(first->pMethods->xShmBarrier != NULL);
    REQUIRE(first->pMethods->xShmUnmap != NULL);
    REQUIRE(first->pMethods->xFetch == NULL);
    REQUIRE(first->pMethods->xRead(first, buffer, 16, 0) == SQLITE_IOERR_SHORT_READ);
    for (int i = 0; i < 16; ++i)
        REQUIRE(buffer[i] == 0);
    REQUIRE(first->pMethods->xWrite(first, "12345678", 8, 4096) == SQLITE_OK);
    REQUIRE(first->pMethods->xFileSize(first, &size) == SQLITE_OK && size == 4104);
    memset(buffer, 0xff, sizeof(buffer));
    REQUIRE(first->pMethods->xRead(first, buffer, 16, 4096) == SQLITE_IOERR_SHORT_READ);
    REQUIRE(memcmp(buffer, "12345678", 8) == 0);
    for (int i = 8; i < 16; ++i)
        REQUIRE(buffer[i] == 0);
    REQUIRE(first->pMethods->xRead(first, buffer, 1, -1) == SQLITE_IOERR_READ);
    REQUIRE(first->pMethods->xWrite(first, buffer, 1, -1) == SQLITE_IOERR_WRITE);
    REQUIRE(first->pMethods->xTruncate(first, -1) == SQLITE_IOERR_TRUNCATE);
    REQUIRE(first->pMethods->xTruncate(first, 3) == SQLITE_OK);
    REQUIRE(first->pMethods->xFileSize(first, &size) == SQLITE_OK && size == 3);
    REQUIRE(first->pMethods->xSync(first, SQLITE_SYNC_FULL) == SQLITE_OK);
    REQUIRE(first->pMethods->xSync(first, SQLITE_SYNC_NORMAL | SQLITE_SYNC_DATAONLY) == SQLITE_OK);
    REQUIRE(first->pMethods->xFileControl(first, 999999, &result) == SQLITE_NOTFOUND);
    REQUIRE(first->pMethods->xFileControl(first, SQLITE_FCNTL_HAS_MOVED, &result) == SQLITE_OK);
    REQUIRE(result == 0);
    REQUIRE(vfs->xOpen(vfs, "native.db", second, flags, NULL) == SQLITE_OK);
    REQUIRE(first->pMethods->xLock(first, SQLITE_LOCK_SHARED) == SQLITE_OK);
    REQUIRE(second->pMethods->xLock(second, SQLITE_LOCK_SHARED) == SQLITE_OK);
    REQUIRE(first->pMethods->xLock(first, SQLITE_LOCK_RESERVED) == SQLITE_OK);
    REQUIRE(second->pMethods->xCheckReservedLock(second, &reserved) == SQLITE_OK && reserved);
    REQUIRE(first->pMethods->xLock(first, SQLITE_LOCK_EXCLUSIVE) == SQLITE_BUSY);
    REQUIRE(second->pMethods->xUnlock(second, SQLITE_LOCK_NONE) == SQLITE_OK);
    REQUIRE(first->pMethods->xLock(first, SQLITE_LOCK_EXCLUSIVE) == SQLITE_OK);
    REQUIRE(first->pMethods->xUnlock(first, SQLITE_LOCK_SHARED) == SQLITE_OK);
    REQUIRE(second->pMethods->xCheckReservedLock(second, &reserved) == SQLITE_OK && !reserved);
    REQUIRE(first->pMethods->xUnlock(first, SQLITE_LOCK_NONE) == SQLITE_OK);
    REQUIRE(first->pMethods->xShmMap(first, -1, 32768, 1, &map1) == SQLITE_IOERR_SHMMAP);
    REQUIRE(map1 == NULL);
    REQUIRE(first->pMethods->xShmMap(first, 0, 4096, 1, &map1) == SQLITE_IOERR_SHMMAP);
    REQUIRE(first->pMethods->xShmMap(first, 128, 32768, 1, &map1) == SQLITE_IOERR_SHMMAP);
    REQUIRE(first->pMethods->xShmMap(first, 0, 32768, 0, &map1) == SQLITE_OK);
    REQUIRE(map1 == NULL);
    REQUIRE(first->pMethods->xShmMap(first, 0, 32768, 1, &map1) == SQLITE_OK);
    REQUIRE(map1 != NULL);
    ((volatile unsigned char *)map1)[1000] = 73;
    first->pMethods->xShmBarrier(first);
    REQUIRE(second->pMethods->xShmMap(second, 0, 32768, 0, &map2) == SQLITE_OK);
    REQUIRE(map2 != NULL && ((volatile unsigned char *)map2)[1000] == 73);
    REQUIRE(second->pMethods->xShmMap(second, 1, 32768, 1, &other_region) == SQLITE_OK);
    REQUIRE(other_region != NULL);
    ((volatile unsigned char *)other_region)[123] = 19;
    REQUIRE(first->pMethods->xShmMap(first, 1, 32768, 0, &other_region) == SQLITE_OK);
    REQUIRE(((volatile unsigned char *)other_region)[123] == 19);
    REQUIRE(first->pMethods->xShmLock(first, 0, 1, SQLITE_SHM_LOCK | SQLITE_SHM_SHARED) == SQLITE_OK);
    REQUIRE(second->pMethods->xShmLock(second, 0, 1, SQLITE_SHM_LOCK | SQLITE_SHM_SHARED) == SQLITE_OK);
    REQUIRE(first->pMethods->xShmLock(first, 0, 1, SQLITE_SHM_LOCK | SQLITE_SHM_EXCLUSIVE) == SQLITE_BUSY);
    REQUIRE(second->pMethods->xShmLock(second, 0, 1, SQLITE_SHM_UNLOCK | SQLITE_SHM_SHARED) == SQLITE_OK);
    REQUIRE(first->pMethods->xShmLock(first, 0, 1, SQLITE_SHM_UNLOCK | SQLITE_SHM_SHARED) == SQLITE_OK);
    REQUIRE(first->pMethods->xShmLock(first, 0, 2, SQLITE_SHM_LOCK | SQLITE_SHM_EXCLUSIVE) == SQLITE_OK);
    REQUIRE(second->pMethods->xShmLock(second, 1, 1, SQLITE_SHM_LOCK | SQLITE_SHM_EXCLUSIVE) == SQLITE_BUSY);
    REQUIRE(second->pMethods->xShmLock(second, 8, 1, SQLITE_SHM_LOCK | SQLITE_SHM_EXCLUSIVE) == SQLITE_IOERR_SHMLOCK);
    REQUIRE(second->pMethods->xShmLock(second, 0, 2, SQLITE_SHM_LOCK | SQLITE_SHM_SHARED) == SQLITE_IOERR_SHMLOCK);
    REQUIRE(second->pMethods->xShmLock(second, 0, 1, 0) == SQLITE_IOERR_SHMLOCK);
    /* A peer requesting deletion must not unlink a live shared-memory inode. */
    REQUIRE(second->pMethods->xShmUnmap(second, 1) == SQLITE_OK);
    REQUIRE(vfs->xAccess(vfs, "native.db-shm", SQLITE_ACCESS_EXISTS, &result) == SQLITE_OK && result);
    REQUIRE(second->pMethods->xClose(second) == SQLITE_OK);
    REQUIRE(vfs->xOpen(vfs, "native.db", second, flags, NULL) == SQLITE_OK);
    REQUIRE(second->pMethods->xShmMap(second, 0, 32768, 0, &map2) == SQLITE_OK);
    REQUIRE(map2 != NULL && ((volatile unsigned char *)map2)[1000] == 73);
    REQUIRE(second->pMethods->xShmLock(second, 0, 1, SQLITE_SHM_LOCK | SQLITE_SHM_EXCLUSIVE) == SQLITE_BUSY);
    REQUIRE(first->pMethods->xShmLock(first, 0, 2, SQLITE_SHM_UNLOCK | SQLITE_SHM_EXCLUSIVE) == SQLITE_OK);
    REQUIRE(second->pMethods->xShmLock(second, 0, 2, SQLITE_SHM_LOCK | SQLITE_SHM_EXCLUSIVE) == SQLITE_OK);
    REQUIRE(second->pMethods->xShmUnmap(second, 0) == SQLITE_OK);
    REQUIRE(second->pMethods->xClose(second) == SQLITE_OK);
    REQUIRE(first->pMethods->xShmUnmap(first, 0) == SQLITE_OK);
    /* With every dead-man holder gone, stale indexes must be truncated on reopen. */
    REQUIRE(first->pMethods->xShmMap(first, 0, 32768, 0, &map1) == SQLITE_OK && !map1);
    REQUIRE(first->pMethods->xShmMap(first, 0, 32768, 1, &map1) == SQLITE_OK && map1);
    REQUIRE(((volatile unsigned char *)map1)[1000] == 0);
    REQUIRE(first->pMethods->xShmUnmap(first, 1) == SQLITE_OK);
    REQUIRE(vfs->xAccess(vfs, "native.db-shm", SQLITE_ACCESS_EXISTS, &result) == SQLITE_OK && !result);
    REQUIRE(vfs->xOpen(vfs, "native.db-wal", second,
                      SQLITE_OPEN_WAL | SQLITE_OPEN_CREATE | SQLITE_OPEN_READWRITE,
                      NULL) == SQLITE_OK);
    REQUIRE(second->pMethods->xWrite(second, "wal", 3, 0) == SQLITE_OK);
    REQUIRE(second->pMethods->xSync(second, SQLITE_SYNC_FULL) == SQLITE_OK);
    REQUIRE(second->pMethods->xClose(second) == SQLITE_OK);
    REQUIRE(vfs->xDelete(vfs, "native.db-wal", 1) == SQLITE_OK);
    const int unsupported[] = {
        SQLITE_OPEN_WAL, SQLITE_OPEN_TEMP_DB, SQLITE_OPEN_TEMP_JOURNAL,
        SQLITE_OPEN_TRANSIENT_DB, SQLITE_OPEN_SUBJOURNAL, SQLITE_OPEN_SUPER_JOURNAL
    };
    for (unsigned i = 0; i < sizeof(unsupported) / sizeof(unsupported[0]); ++i) {
        REQUIRE(vfs->xOpen(vfs, "unsupported", second,
                          unsupported[i] | SQLITE_OPEN_CREATE | SQLITE_OPEN_READWRITE,
                          NULL) == SQLITE_CANTOPEN);
        REQUIRE(second->pMethods == NULL);
    }
    REQUIRE(vfs->xOpen(vfs, NULL, second, flags, NULL) == SQLITE_CANTOPEN);
    REQUIRE(second->pMethods == NULL);
    REQUIRE(vfs->xOpen(vfs, "native.db-wal", second, flags, NULL) == SQLITE_CANTOPEN);
    REQUIRE(vfs->xOpen(vfs, "native.db-shm", second, flags, NULL) == SQLITE_CANTOPEN);
    REQUIRE(vfs->xOpen(vfs, "delete-on-close.db", second,
                      flags | SQLITE_OPEN_DELETEONCLOSE, NULL) == SQLITE_CANTOPEN);
    REQUIRE(vfs->xOpen(vfs, "native.db", second, flags | SQLITE_OPEN_EXCLUSIVE,
                      NULL) == SQLITE_CANTOPEN);
    REQUIRE(second->pMethods == NULL);
    REQUIRE(vfs->xAccess(vfs, "native.db", 999, &result) == SQLITE_IOERR_ACCESS);
    REQUIRE(result == 0);
    REQUIRE(vfs->xFullPathname(vfs, "../escape", sizeof(pathname), pathname) == SQLITE_CANTOPEN);
    REQUIRE(vfs->xFullPathname(vfs, "native.db", sizeof(pathname), pathname) == SQLITE_CANTOPEN);
    REQUIRE(vfs->xDelete(vfs, "../escape", 0) == SQLITE_IOERR_DELETE);
    REQUIRE(vfs->xDelete(vfs, "native.db-shm", 0) == SQLITE_IOERR_DELETE);
    REQUIRE(first->pMethods->xClose(first) == SQLITE_OK);
    REQUIRE(vfs->xDelete(vfs, "native.db", 1) == SQLITE_OK);
    REQUIRE(vfs->xDelete(vfs, "native.db", 1) == SQLITE_OK);
cleanup:
    if (first && first->pMethods)
        first->pMethods->xClose(first);
    if (second && second->pMethods)
        second->pMethods->xClose(second);
    sqlite3_free(first);
    sqlite3_free(second);
    if (failure)
        sqlite3_result_error(context, failure, -1);
    else
        sqlite3_result_text(context, "rollback and WAL/SHM ABI OK", -1, SQLITE_STATIC);
}

int sqlite3_testprobe_init(sqlite3 *db, char **error, const sqlite3_api_routines *api) {
    SQLITE_EXTENSION_INIT2(api);
    (void)error;
    return sqlite3_create_function(db, "dirfd_vfs_native_probe", 0,
                                    SQLITE_UTF8 | SQLITE_DIRECTONLY,
                                    NULL, probe, NULL, NULL);
}
