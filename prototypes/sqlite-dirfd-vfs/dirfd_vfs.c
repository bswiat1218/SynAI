#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64
#include <sqlite3ext.h>
static SQLITE_EXTENSION_INIT1

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/magic.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <sys/random.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/vfs.h>
#include <time.h>
#include <unistd.h>

#ifndef F_OFD_SETLK
#error "This prototype requires Linux open-file-description locks"
#endif

#define VFS_NAME "dirfd_prototype"
#define PENDING_BYTE ((off_t)0x40000000)
#define RESERVED_BYTE (PENDING_BYTE + 1)
#define SHARED_FIRST (PENDING_BYTE + 2)
#define SHARED_SIZE 510
#define NAME_LIMIT 200
#define SHM_BASE 120
#define SHM_DMS (SHM_BASE + SQLITE_SHM_NLOCK)
#define SHM_GROW (SHM_DMS + 1)
#define SHM_REGION_SIZE 32768
#define SHM_REGIONS 128

typedef struct DirFile {
    sqlite3_file base;
    int fd;
    int lock;
    int created;
    int type;
    int readonly;
    int shm_fd;
    int wal_known;
    dev_t wal_dev;
    ino_t wal_ino;
    int journal_known;
    dev_t journal_dev;
    ino_t journal_ino;
    struct DirFile *next;
    void *regions[SHM_REGIONS];
    char name[NAME_LIMIT + 1];
    char shm_name[NAME_LIMIT + 1];
} DirFile;

/* The extension and this root capability intentionally live until process exit. */
static int root_fd = -1;
static sqlite3_mutex *root_mutex;
static sqlite3_vfs dir_vfs;
static DirFile *live_files;

static int ends_with(const char *name, const char *suffix) {
    size_t n = strlen(name), s = strlen(suffix);
    return n >= s && strcmp(name + n - s, suffix) == 0;
}

static int valid_name(const char *name) {
    if (!name || !name[0] || name[0] == '.' || strlen(name) > NAME_LIMIT)
        return 0;
    for (const unsigned char *p = (const unsigned char *)name; *p; ++p) {
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '_' || *p == '-' || *p == '.'))
            return 0;
    }
    return 1;
}

static int reserved_sidecar(const char *name) {
    return ends_with(name, "-wal") || ends_with(name, "-shm");
}

static int private_directory(int fd, struct stat *st) {
    return fstat(fd, st) == 0 && S_ISDIR(st->st_mode) &&
           st->st_uid == geteuid() && (st->st_mode & 0077) == 0;
}

static int root(void) {
    int fd;
    sqlite3_mutex_enter(root_mutex);
    fd = root_fd;
    sqlite3_mutex_leave(root_mutex);
    struct stat st;
    if (fd >= 0 && !private_directory(fd, &st))
        return -1;
    return fd;
}

static int safe_stat(int dir, const char *name, struct stat *st) {
    if (fstatat(dir, name, st, AT_SYMLINK_NOFOLLOW) < 0)
        return 0;
    if (!S_ISREG(st->st_mode) || st->st_nlink != 1) {
        errno = EINVAL;
        return 0;
    }
    return 1;
}

static int same_file(int fd, const char *name) {
    struct stat held, named;
    return fstat(fd, &held) == 0 && held.st_nlink == 1 &&
           safe_stat(root(), name, &named) &&
           held.st_dev == named.st_dev && held.st_ino == named.st_ino;
}

static int fd_lock(int fd, short type, off_t start, off_t length) {
    struct flock lk = {.l_type = type, .l_whence = SEEK_SET,
                       .l_start = start, .l_len = length};
    int rc;
    do {
        rc = fcntl(fd, F_OFD_SETLK, &lk);
    } while (rc < 0 && errno == EINTR);
    if (rc == 0)
        return SQLITE_OK;
    return errno == EAGAIN || errno == EACCES ? SQLITE_BUSY : SQLITE_IOERR_LOCK;
}

static int set_lock(DirFile *f, short type, off_t start, off_t length) {
    return fd_lock(f->fd, type, start, length);
}

/* Local identity checks avoid knowingly joining a replacement inode while an
   existing connection retains authority. They do not close hostile mutation races. */
static int join_file(DirFile *f, const struct stat *st) {
    int rc = SQLITE_OK;
    sqlite3_mutex_enter(root_mutex);
    for (DirFile *peer = live_files; peer; peer = peer->next) {
        if (strcmp(peer->name, f->name) == 0) {
            struct stat held;
            if (fstat(peer->fd, &held) < 0 ||
                held.st_dev != st->st_dev || held.st_ino != st->st_ino) {
                rc = SQLITE_CANTOPEN;
                break;
            }
        }
        if (peer->type == SQLITE_OPEN_MAIN_DB) {
            size_t suffix = f->type == SQLITE_OPEN_WAL ? 4 :
                            f->type == SQLITE_OPEN_MAIN_JOURNAL ? 8 : 0;
            if (suffix && strlen(f->name) == strlen(peer->name) + suffix &&
                memcmp(peer->name, f->name, strlen(peer->name)) == 0) {
                int known = suffix == 4 ? peer->wal_known : peer->journal_known;
                dev_t dev = suffix == 4 ? peer->wal_dev : peer->journal_dev;
                ino_t ino = suffix == 4 ? peer->wal_ino : peer->journal_ino;
                if (known && (dev != st->st_dev || ino != st->st_ino)) {
                    rc = SQLITE_CANTOPEN;
                    break;
                }
            }
        }
    }
    if (rc == SQLITE_OK) {
        f->next = live_files;
        live_files = f;
        if (f->type == SQLITE_OPEN_WAL || f->type == SQLITE_OPEN_MAIN_JOURNAL) {
            size_t prefix = strlen(f->name) - (f->type == SQLITE_OPEN_WAL ? 4 : 8);
            for (DirFile *peer = live_files; peer; peer = peer->next) {
                if (peer->type == SQLITE_OPEN_MAIN_DB && strlen(peer->name) == prefix &&
                    memcmp(peer->name, f->name, prefix) == 0) {
                    if (f->type == SQLITE_OPEN_WAL) {
                        peer->wal_known = 1;
                        peer->wal_dev = st->st_dev;
                        peer->wal_ino = st->st_ino;
                    } else {
                        peer->journal_known = 1;
                        peer->journal_dev = st->st_dev;
                        peer->journal_ino = st->st_ino;
                    }
                }
            }
        }
    }
    sqlite3_mutex_leave(root_mutex);
    return rc;
}

static int file_shm_unmap(sqlite3_file *file, int delete_flag);

static int file_close(sqlite3_file *file) {
    DirFile *f = (DirFile *)file;
    int shm_rc = file_shm_unmap(file, 0);
    /* Do not retry close on Linux: EINTR already releases the descriptor. */
    sqlite3_mutex_enter(root_mutex);
    int rc = close(f->fd);
    DirFile **link = &live_files;
    while (*link && *link != f)
        link = &(*link)->next;
    if (*link)
        *link = f->next;
    sqlite3_mutex_leave(root_mutex);
    f->fd = -1;
    file->pMethods = NULL;
    return shm_rc != SQLITE_OK ? shm_rc : rc == 0 ? SQLITE_OK : SQLITE_IOERR_CLOSE;
}

static int file_read(sqlite3_file *file, void *buffer, int amount,
                     sqlite3_int64 offset) {
    DirFile *f = (DirFile *)file;
    if (!same_file(f->fd, f->name))
        return SQLITE_IOERR_READ;
    if (amount < 0 || offset < 0 || offset > INT64_MAX - amount)
        return SQLITE_IOERR_READ;
    int done = 0;
    while (done < amount) {
        ssize_t n = pread(f->fd, (char *)buffer + done, (size_t)(amount - done),
                          (off_t)(offset + done));
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0)
            return SQLITE_IOERR_READ;
        if (n == 0) {
            memset((char *)buffer + done, 0, (size_t)(amount - done));
            return SQLITE_IOERR_SHORT_READ;
        }
        done += (int)n;
    }
    return SQLITE_OK;
}

static int file_write(sqlite3_file *file, const void *buffer, int amount,
                      sqlite3_int64 offset) {
    DirFile *f = (DirFile *)file;
    if (!same_file(f->fd, f->name))
        return SQLITE_IOERR_WRITE;
    if (amount < 0 || offset < 0 || offset > INT64_MAX - amount)
        return SQLITE_IOERR_WRITE;
    int done = 0;
    while (done < amount) {
        ssize_t n = pwrite(f->fd, (const char *)buffer + done,
                           (size_t)(amount - done), (off_t)(offset + done));
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return errno == ENOSPC ? SQLITE_FULL : SQLITE_IOERR_WRITE;
        done += (int)n;
    }
    return SQLITE_OK;
}

static int file_truncate(sqlite3_file *file, sqlite3_int64 size) {
    DirFile *f = (DirFile *)file;
    if (!same_file(f->fd, f->name))
        return SQLITE_IOERR_TRUNCATE;
    if (size < 0)
        return SQLITE_IOERR_TRUNCATE;
    int rc;
    do {
        rc = ftruncate(((DirFile *)file)->fd, (off_t)size);
    } while (rc < 0 && errno == EINTR);
    return rc == 0 ? SQLITE_OK : SQLITE_IOERR_TRUNCATE;
}

static int sync_fd(int fd, int data_only) {
    int rc;
    do {
        rc = data_only ? fdatasync(fd) : fsync(fd);
    } while (rc < 0 && errno == EINTR);
    return rc;
}

static int file_sync(sqlite3_file *file, int flags) {
    DirFile *f = (DirFile *)file;
    if (!same_file(f->fd, f->name))
        return SQLITE_IOERR_FSYNC;
    if (sync_fd(f->fd, (flags & SQLITE_SYNC_DATAONLY) != 0) < 0)
        return SQLITE_IOERR_FSYNC;
    if (f->created) {
        if (sync_fd(root(), 0) < 0)
            return SQLITE_IOERR_DIR_FSYNC;
        f->created = 0;
    }
    return SQLITE_OK;
}

static int file_size(sqlite3_file *file, sqlite3_int64 *size) {
    DirFile *f = (DirFile *)file;
    if (!same_file(f->fd, f->name))
        return SQLITE_IOERR_FSTAT;
    struct stat st;
    if (fstat(((DirFile *)file)->fd, &st) < 0)
        return SQLITE_IOERR_FSTAT;
    *size = st.st_size;
    return SQLITE_OK;
}

static int file_lock(sqlite3_file *file, int requested) {
    DirFile *f = (DirFile *)file;
    int rc;
    if (!same_file(f->fd, f->name))
        return SQLITE_IOERR_LOCK;
    if (requested < SQLITE_LOCK_SHARED || requested > SQLITE_LOCK_EXCLUSIVE)
        return SQLITE_IOERR_LOCK;
    if (requested <= f->lock)
        return SQLITE_OK;
    if (f->lock == SQLITE_LOCK_NONE) {
        rc = set_lock(f, F_RDLCK, PENDING_BYTE, 1);
        if (rc != SQLITE_OK)
            return rc;
        rc = set_lock(f, F_RDLCK, SHARED_FIRST, SHARED_SIZE);
        int release = set_lock(f, F_UNLCK, PENDING_BYTE, 1);
        if (rc == SQLITE_OK)
            f->lock = SQLITE_LOCK_SHARED;
        if (release != SQLITE_OK)
            return SQLITE_IOERR_UNLOCK;
        if (rc != SQLITE_OK)
            return rc;
    }
    if (requested == SQLITE_LOCK_SHARED)
        return SQLITE_OK;
    if (requested == SQLITE_LOCK_RESERVED) {
        rc = set_lock(f, F_WRLCK, RESERVED_BYTE, 1);
        if (rc == SQLITE_OK)
            f->lock = SQLITE_LOCK_RESERVED;
        return rc;
    }
    if (f->lock < SQLITE_LOCK_PENDING) {
        rc = set_lock(f, F_WRLCK, PENDING_BYTE, 1);
        if (rc != SQLITE_OK)
            return rc;
        f->lock = SQLITE_LOCK_PENDING;
    }
    if (requested == SQLITE_LOCK_PENDING)
        return SQLITE_OK;
    rc = set_lock(f, F_WRLCK, SHARED_FIRST, SHARED_SIZE);
    if (rc == SQLITE_OK)
        f->lock = SQLITE_LOCK_EXCLUSIVE;
    return rc;
}

static int file_unlock(sqlite3_file *file, int requested) {
    DirFile *f = (DirFile *)file;
    int rc;
    if (requested != SQLITE_LOCK_NONE && requested != SQLITE_LOCK_SHARED)
        return SQLITE_IOERR_UNLOCK;
    if (requested >= f->lock)
        return SQLITE_OK;
    if (requested == SQLITE_LOCK_NONE) {
        rc = set_lock(f, F_UNLCK, PENDING_BYTE, SHARED_SIZE + 2);
    } else {
        rc = set_lock(f, F_RDLCK, SHARED_FIRST, SHARED_SIZE);
        if (rc == SQLITE_OK)
            rc = set_lock(f, F_UNLCK, PENDING_BYTE, 2);
    }
    if (rc != SQLITE_OK)
        return SQLITE_IOERR_UNLOCK;
    f->lock = requested;
    return SQLITE_OK;
}

static int file_reserved(sqlite3_file *file, int *reserved) {
    DirFile *f = (DirFile *)file;
    *reserved = 0;
    if (!same_file(f->fd, f->name))
        return SQLITE_IOERR_CHECKRESERVEDLOCK;
    struct flock lk = {.l_type = F_WRLCK, .l_whence = SEEK_SET,
                       .l_start = RESERVED_BYTE, .l_len = 1};
    *reserved = f->lock >= SQLITE_LOCK_RESERVED;
    if (*reserved)
        return SQLITE_OK;
    if (fcntl(f->fd, F_OFD_GETLK, &lk) < 0)
        return SQLITE_IOERR_CHECKRESERVEDLOCK;
    *reserved = lk.l_type != F_UNLCK;
    return SQLITE_OK;
}

static int file_control(sqlite3_file *file, int operation, void *argument) {
    DirFile *f = (DirFile *)file;
    if (operation == SQLITE_FCNTL_LOCKSTATE) {
        *(int *)argument = f->lock;
        return SQLITE_OK;
    }
    if (operation == SQLITE_FCNTL_HAS_MOVED) {
        struct stat held, named;
        if (fstat(f->fd, &held) < 0)
            return SQLITE_IOERR_FSTAT;
        *(int *)argument = !safe_stat(root(), f->name, &named) ||
                           held.st_dev != named.st_dev || held.st_ino != named.st_ino;
        return SQLITE_OK;
    }
    return SQLITE_NOTFOUND;
}

static int file_sector(sqlite3_file *file) {
    (void)file;
    return 4096;
}

static int file_characteristics(sqlite3_file *file) {
    (void)file;
    return 0;
}

static int open_shm_file(DirFile *f) {
    if (f->type != SQLITE_OPEN_MAIN_DB || f->readonly)
        return SQLITE_IOERR_SHMOPEN;
    if (!same_file(f->fd, f->name))
        return SQLITE_IOERR_SHMOPEN;
    if (f->shm_fd >= 0)
        return same_file(f->shm_fd, f->shm_name) ? SQLITE_OK : SQLITE_IOERR_SHMOPEN;
    if (strlen(f->name) + 4 > NAME_LIMIT)
        return SQLITE_IOERR_SHMOPEN;
    sqlite3_snprintf(sizeof(f->shm_name), f->shm_name, "%s-shm", f->name);
    int fd = openat(root(), f->shm_name,
                    O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK,
                    0600);
    if (fd < 0 && errno == EEXIST)
        fd = openat(root(), f->shm_name, O_RDWR | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0)
        return SQLITE_IOERR_SHMOPEN;
    int rc = SQLITE_IOERR_SHMOPEN;
    if (!same_file(fd, f->shm_name))
        goto fail;
    struct stat current;
    if (fstat(fd, &current) < 0)
        goto fail;
    sqlite3_mutex_enter(root_mutex);
    for (DirFile *peer = live_files; peer; peer = peer->next) {
        if (peer->shm_fd >= 0 && strcmp(peer->shm_name, f->shm_name) == 0) {
            struct stat held;
            if (fstat(peer->shm_fd, &held) < 0 ||
                held.st_dev != current.st_dev || held.st_ino != current.st_ino) {
                sqlite3_mutex_leave(root_mutex);
                goto fail;
            }
        }
    }
    sqlite3_mutex_leave(root_mutex);
    /* The Unix VFS dead-man byte: reset stale indexes only with no live holders.
       Each connection owns a separate OFD, so a close cannot drop a peer's locks. */
    rc = fd_lock(fd, F_WRLCK, SHM_DMS, 1);
    if (rc == SQLITE_OK) {
        int truncated;
        do {
            truncated = ftruncate(fd, 0);
        } while (truncated < 0 && errno == EINTR);
        if (truncated < 0) {
            rc = SQLITE_IOERR_SHMSIZE;
            goto fail;
        }
    } else if (rc != SQLITE_BUSY) {
        rc = SQLITE_IOERR_SHMLOCK;
        goto fail;
    }
    rc = fd_lock(fd, F_RDLCK, SHM_DMS, 1);
    if (rc != SQLITE_OK) {
        if (rc != SQLITE_BUSY)
            rc = SQLITE_IOERR_SHMLOCK;
        goto fail;
    }
    if (!same_file(fd, f->shm_name)) {
        rc = SQLITE_IOERR_SHMOPEN;
        goto fail;
    }
    sqlite3_mutex_enter(root_mutex);
    f->shm_fd = fd;
    sqlite3_mutex_leave(root_mutex);
    return SQLITE_OK;
fail:
    close(fd);
    return rc;
}

static int file_shm_map(sqlite3_file *file, int region, int size, int extend,
                        void volatile **out) {
    DirFile *f = (DirFile *)file;
    *out = NULL;
    long page = sysconf(_SC_PAGESIZE);
    if (region < 0 || region >= SHM_REGIONS || size != SHM_REGION_SIZE ||
        page <= 0 || SHM_REGION_SIZE % page != 0 || (extend != 0 && extend != 1))
        return SQLITE_IOERR_SHMMAP;
    int rc = open_shm_file(f);
    if (rc != SQLITE_OK)
        return rc;
    if (f->regions[region]) {
        *out = f->regions[region];
        return SQLITE_OK;
    }
    /* Serialize growth: never shrink a file that a peer has already enlarged. */
    rc = fd_lock(f->shm_fd, F_WRLCK, SHM_GROW, 1);
    if (rc != SQLITE_OK)
        return rc == SQLITE_BUSY ? rc : SQLITE_IOERR_SHMLOCK;
    struct stat st;
    off_t needed = (off_t)(region + 1) * size;
    if (fstat(f->shm_fd, &st) < 0) {
        rc = SQLITE_IOERR_SHMSIZE;
    } else if (st.st_size < needed) {
        if (!extend) {
            rc = SQLITE_OK;
            goto release;
        }
        int truncated;
        do {
            truncated = ftruncate(f->shm_fd, needed);
        } while (truncated < 0 && errno == EINTR);
        if (truncated < 0)
            rc = SQLITE_IOERR_SHMSIZE;
    }
    if (rc == SQLITE_OK) {
        void *mapped = mmap(NULL, (size_t)size, PROT_READ | PROT_WRITE, MAP_SHARED,
                            f->shm_fd, (off_t)region * size);
        if (mapped == MAP_FAILED) {
            rc = SQLITE_IOERR_SHMMAP;
        } else {
            f->regions[region] = mapped;
            *out = mapped;
        }
    }
release:
    if (fd_lock(f->shm_fd, F_UNLCK, SHM_GROW, 1) != SQLITE_OK) {
        *out = NULL;
        return SQLITE_IOERR_SHMLOCK;
    }
    return rc;
}

static int file_shm_lock(sqlite3_file *file, int offset, int count, int flags) {
    DirFile *f = (DirFile *)file;
    if (offset < 0 || count < 1 || offset > SQLITE_SHM_NLOCK - count ||
        (flags != (SQLITE_SHM_LOCK | SQLITE_SHM_SHARED) &&
         flags != (SQLITE_SHM_LOCK | SQLITE_SHM_EXCLUSIVE) &&
         flags != (SQLITE_SHM_UNLOCK | SQLITE_SHM_SHARED) &&
         flags != (SQLITE_SHM_UNLOCK | SQLITE_SHM_EXCLUSIVE)) ||
        ((flags & SQLITE_SHM_SHARED) && count != 1))
        return SQLITE_IOERR_SHMLOCK;
    if (f->shm_fd < 0)
        return SQLITE_IOERR_SHMLOCK;
    /* Unlock remains available on errors so a failed transaction releases authority. */
    if ((flags & SQLITE_SHM_LOCK) &&
        (!same_file(f->fd, f->name) || !same_file(f->shm_fd, f->shm_name)))
        return SQLITE_IOERR_SHMLOCK;
    short type = (flags & SQLITE_SHM_UNLOCK) ? F_UNLCK :
                 (flags & SQLITE_SHM_EXCLUSIVE) ? F_WRLCK : F_RDLCK;
    int rc = fd_lock(f->shm_fd, type, SHM_BASE + offset, count);
    return rc == SQLITE_OK || rc == SQLITE_BUSY ? rc : SQLITE_IOERR_SHMLOCK;
}

static void file_shm_barrier(sqlite3_file *file) {
    (void)file;
    atomic_thread_fence(memory_order_seq_cst);
}

static int file_shm_unmap(sqlite3_file *file, int delete_flag) {
    DirFile *f = (DirFile *)file;
    int rc = SQLITE_OK;
    for (int i = 0; i < SHM_REGIONS; ++i) {
        if (f->regions[i]) {
            if (munmap(f->regions[i], SHM_REGION_SIZE) < 0)
                rc = SQLITE_IOERR_SHMMAP;
            f->regions[i] = NULL;
        }
    }
    if (f->shm_fd >= 0) {
        if (fd_lock(f->shm_fd, F_UNLCK, SHM_BASE, SQLITE_SHM_NLOCK) != SQLITE_OK)
            rc = SQLITE_IOERR_SHMLOCK;
        if (delete_flag) {
            int locked = fd_lock(f->shm_fd, F_WRLCK, SHM_DMS, 1);
            if (locked == SQLITE_OK) {
                /* Identity check is not a defence against a hostile directory writer. */
                if (!same_file(f->shm_fd, f->shm_name) ||
                    unlinkat(root(), f->shm_name, 0) < 0)
                    rc = SQLITE_IOERR_DELETE;
            } else if (locked != SQLITE_BUSY) {
                rc = SQLITE_IOERR_SHMLOCK;
            }
        }
        sqlite3_mutex_enter(root_mutex);
        if (close(f->shm_fd) < 0)
            rc = SQLITE_IOERR_CLOSE;
        f->shm_fd = -1;
        sqlite3_mutex_leave(root_mutex);
    }
    return rc;
}

/* Version 2 offers SHM but still no database mmap (xFetch). */
static const sqlite3_io_methods methods = {
    .iVersion = 2, .xClose = file_close, .xRead = file_read,
    .xWrite = file_write, .xTruncate = file_truncate, .xSync = file_sync,
    .xFileSize = file_size, .xLock = file_lock, .xUnlock = file_unlock,
    .xCheckReservedLock = file_reserved, .xFileControl = file_control,
    .xSectorSize = file_sector, .xDeviceCharacteristics = file_characteristics,
    .xShmMap = file_shm_map, .xShmLock = file_shm_lock,
    .xShmBarrier = file_shm_barrier, .xShmUnmap = file_shm_unmap
};

static int vfs_open(sqlite3_vfs *vfs, sqlite3_filename name, sqlite3_file *file,
                    int flags, int *out_flags) {
    (void)vfs;
    DirFile *f = (DirFile *)file;
    memset(f, 0, sizeof(*f));
    f->fd = -1;
    f->shm_fd = -1;
    int dir = root();
    int type = flags & (SQLITE_OPEN_MAIN_DB | SQLITE_OPEN_MAIN_JOURNAL |
                       SQLITE_OPEN_TEMP_DB | SQLITE_OPEN_TEMP_JOURNAL |
                       SQLITE_OPEN_TRANSIENT_DB | SQLITE_OPEN_SUBJOURNAL |
                       SQLITE_OPEN_SUPER_JOURNAL | SQLITE_OPEN_WAL);
    if (dir < 0 || !valid_name(name) ||
        (type != SQLITE_OPEN_MAIN_DB && type != SQLITE_OPEN_MAIN_JOURNAL &&
         type != SQLITE_OPEN_WAL) ||
        (flags & (SQLITE_OPEN_DELETEONCLOSE | SQLITE_OPEN_MEMORY)) ||
        ((flags & SQLITE_OPEN_URI) &&
         (sqlite3_uri_boolean(name, "immutable", 0) ||
          sqlite3_uri_boolean(name, "nolock", 0))))
        return SQLITE_CANTOPEN;
    if ((type == SQLITE_OPEN_MAIN_JOURNAL) != ends_with(name, "-journal") ||
        (type == SQLITE_OPEN_WAL) != ends_with(name, "-wal") ||
        ends_with(name, "-shm"))
        return SQLITE_CANTOPEN;
    int oflags = O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK;
    oflags |= (flags & SQLITE_OPEN_READWRITE) ? O_RDWR : O_RDONLY;
    if (flags & SQLITE_OPEN_CREATE) {
        f->fd = openat(dir, name, oflags | O_CREAT | O_EXCL, 0600);
        if (f->fd >= 0)
            f->created = 1;
        else if (errno == EEXIST && !(flags & SQLITE_OPEN_EXCLUSIVE))
            f->fd = openat(dir, name, oflags);
    } else {
        f->fd = openat(dir, name, oflags);
    }
    if (f->fd < 0)
        return SQLITE_CANTOPEN;
    struct stat st;
    struct flock probe = {.l_type = F_WRLCK, .l_whence = SEEK_SET,
                          .l_start = RESERVED_BYTE, .l_len = 1};
    if (fstat(f->fd, &st) < 0 || !S_ISREG(st.st_mode) || st.st_nlink != 1 ||
        fcntl(f->fd, F_OFD_GETLK, &probe) < 0) {
        close(f->fd);
        f->fd = -1;
        if (f->created)
            unlinkat(dir, name, 0);
        return SQLITE_CANTOPEN;
    }
    memcpy(f->name, name, strlen(name) + 1);
    if (!same_file(f->fd, f->name)) {
        close(f->fd);
        f->fd = -1;
        return SQLITE_CANTOPEN;
    }
    f->type = type;
    f->readonly = (flags & SQLITE_OPEN_READWRITE) == 0;
    if (join_file(f, &st) != SQLITE_OK) {
        close(f->fd);
        f->fd = -1;
        return SQLITE_CANTOPEN;
    }
    if (out_flags)
        *out_flags = flags;
    file->pMethods = &methods;
    return SQLITE_OK;
}

static int vfs_delete(sqlite3_vfs *vfs, const char *name, int sync_dir) {
    (void)vfs;
    int dir = root();
    struct stat st;
    if (dir < 0 || !valid_name(name) || ends_with(name, "-shm"))
        return SQLITE_IOERR_DELETE;
    if (!safe_stat(dir, name, &st))
        return errno == ENOENT ? SQLITE_OK : SQLITE_IOERR_DELETE;
    sqlite3_mutex_enter(root_mutex);
    for (DirFile *peer = live_files; peer; peer = peer->next) {
        if (strcmp(peer->name, name) == 0) {
            struct stat held;
            if (fstat(peer->fd, &held) < 0 ||
                held.st_dev != st.st_dev || held.st_ino != st.st_ino) {
                sqlite3_mutex_leave(root_mutex);
                return SQLITE_IOERR_DELETE;
            }
        }
    }
    sqlite3_mutex_leave(root_mutex);
    int sidecar = ends_with(name, "-wal") ? 4 : ends_with(name, "-journal") ? 8 : 0;
    if (sidecar) {
        size_t prefix = strlen(name) - (size_t)sidecar;
        sqlite3_mutex_enter(root_mutex);
        for (DirFile *peer = live_files; peer; peer = peer->next) {
            if (peer->type == SQLITE_OPEN_MAIN_DB &&
                strlen(peer->name) == prefix && memcmp(peer->name, name, prefix) == 0) {
                int known = sidecar == 4 ? peer->wal_known : peer->journal_known;
                dev_t dev = sidecar == 4 ? peer->wal_dev : peer->journal_dev;
                ino_t ino = sidecar == 4 ? peer->wal_ino : peer->journal_ino;
                if (known && (dev != st.st_dev || ino != st.st_ino)) {
                    sqlite3_mutex_leave(root_mutex);
                    return SQLITE_IOERR_DELETE;
                }
            }
        }
        sqlite3_mutex_leave(root_mutex);
    }
    if (unlinkat(dir, name, 0) < 0)
        return errno == ENOENT ? SQLITE_OK : SQLITE_IOERR_DELETE;
    if (sidecar) {
        size_t prefix = strlen(name) - (size_t)sidecar;
        sqlite3_mutex_enter(root_mutex);
        for (DirFile *peer = live_files; peer; peer = peer->next) {
            if (peer->type == SQLITE_OPEN_MAIN_DB &&
                strlen(peer->name) == prefix && memcmp(peer->name, name, prefix) == 0) {
                if (sidecar == 4)
                    peer->wal_known = 0;
                else
                    peer->journal_known = 0;
            }
        }
        sqlite3_mutex_leave(root_mutex);
    }
    return sync_dir && sync_fd(dir, 0) < 0 ? SQLITE_IOERR_DIR_FSYNC : SQLITE_OK;
}

static int vfs_access(sqlite3_vfs *vfs, const char *name, int flags, int *result) {
    (void)vfs;
    *result = 0;
    int dir = root();
    struct stat st;
    if (dir < 0 || !valid_name(name) ||
        (flags != SQLITE_ACCESS_EXISTS && flags != SQLITE_ACCESS_READWRITE &&
         flags != SQLITE_ACCESS_READ))
        return SQLITE_IOERR_ACCESS;
    if (fstatat(dir, name, &st, AT_SYMLINK_NOFOLLOW) < 0)
        return errno == ENOENT ? SQLITE_OK : SQLITE_IOERR_ACCESS;
    if (!S_ISREG(st.st_mode) || st.st_nlink != 1)
        return SQLITE_IOERR_ACCESS;
    if (flags == SQLITE_ACCESS_EXISTS) {
        *result = 1;
    } else {
        int mode = flags == SQLITE_ACCESS_READWRITE ? R_OK | W_OK : R_OK;
        *result = faccessat(dir, name, mode, AT_EACCESS | AT_SYMLINK_NOFOLLOW) == 0;
    }
    return SQLITE_OK;
}

static int vfs_fullpath(sqlite3_vfs *vfs, const char *name, int size, char *out) {
    (void)vfs;
    if (!valid_name(name) || reserved_sidecar(name) || size <= (int)strlen(name))
        return SQLITE_CANTOPEN;
    memcpy(out, name, strlen(name) + 1);
    return SQLITE_OK;
}

static void *vfs_dlopen(sqlite3_vfs *vfs, const char *name) {
    (void)vfs;
    (void)name;
    return NULL;
}

static void vfs_dlerror(sqlite3_vfs *vfs, int size, char *message) {
    (void)vfs;
    sqlite3_snprintf(size, message, "extension loading through dirfd VFS is disabled");
}

static void (*vfs_dlsym(sqlite3_vfs *vfs, void *handle, const char *name))(void) {
    (void)vfs;
    (void)handle;
    (void)name;
    return NULL;
}

static void vfs_dlclose(sqlite3_vfs *vfs, void *handle) {
    (void)vfs;
    (void)handle;
}

static int vfs_random(sqlite3_vfs *vfs, int size, char *out) {
    (void)vfs;
    int done = 0;
    while (done < size) {
        ssize_t n = getrandom(out + done, (size_t)(size - done), 0);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            break;
        done += (int)n;
    }
    return done;
}

static int vfs_sleep(sqlite3_vfs *vfs, int microseconds) {
    (void)vfs;
    if (microseconds <= 0)
        return 0;
    struct timespec delay = {.tv_sec = microseconds / 1000000,
                             .tv_nsec = (microseconds % 1000000) * 1000L};
    while (nanosleep(&delay, &delay) < 0 && errno == EINTR) {}
    return microseconds;
}

static int vfs_time(sqlite3_vfs *vfs, double *julian) {
    (void)vfs;
    struct timeval now;
    if (gettimeofday(&now, NULL) < 0)
        return SQLITE_ERROR;
    *julian = 2440587.5 + ((double)now.tv_sec + now.tv_usec / 1000000.0) / 86400.0;
    return SQLITE_OK;
}

static int vfs_error(sqlite3_vfs *vfs, int size, char *message) {
    (void)vfs;
    int saved = errno;
    sqlite3_snprintf(size, message, "%s", strerror(saved));
    return saved;
}

static void configure_root(sqlite3_context *context, int argc, sqlite3_value **argv) {
    (void)argc;
    sqlite3_int64 borrowed = sqlite3_value_int64(argv[0]);
    if (sqlite3_value_type(argv[0]) != SQLITE_INTEGER || borrowed < 0 ||
        borrowed > INT_MAX) {
        sqlite3_result_error(context, "root must be an integer directory descriptor", -1);
        return;
    }
    int fd = fcntl((int)borrowed, F_DUPFD_CLOEXEC, 0);
    if (fd < 0) {
        sqlite3_result_error(context, "cannot duplicate root descriptor", -1);
        return;
    }
    struct stat candidate, existing;
    struct statfs filesystem;
    int fd_flags = fcntl(fd, F_GETFL);
    if (!private_directory(fd, &candidate) ||
        fd_flags < 0 || (fd_flags & O_PATH) != 0) {
        close(fd);
        sqlite3_result_error(context,
                             "root must be an effective-UID-owned directory, "
                             "private (no group/world permissions) and not O_PATH", -1);
        return;
    }
    if (fstatfs(fd, &filesystem) < 0 || filesystem.f_type != EXT4_SUPER_MAGIC) {
        close(fd);
        sqlite3_result_error(context, "only Linux ext-family root filesystems are supported", -1);
        return;
    }
    sqlite3_mutex_enter(root_mutex);
    if (root_fd >= 0) {
        int same = fstat(root_fd, &existing) == 0 &&
                   candidate.st_dev == existing.st_dev && candidate.st_ino == existing.st_ino;
        sqlite3_mutex_leave(root_mutex);
        close(fd);
        if (!same) {
            sqlite3_result_error(context, "root capability is immutable until process exit", -1);
            return;
        }
    } else {
        root_fd = fd;
        sqlite3_mutex_leave(root_mutex);
    }
    sqlite3_result_text(context, VFS_NAME, -1, SQLITE_STATIC);
}

int sqlite3_dirfdvfs_init(sqlite3 *db, char **error,
                         const sqlite3_api_routines *api) {
    if (sqlite3_api && sqlite3_api != api)
        return SQLITE_MISUSE;
    SQLITE_EXTENSION_INIT2(api);
    (void)error;
    root_mutex = sqlite3_mutex_alloc(SQLITE_MUTEX_STATIC_APP1);
    sqlite3_mutex_enter(root_mutex);
    int rc = SQLITE_OK;
    sqlite3_vfs *registered = sqlite3_vfs_find(VFS_NAME);
    if (registered && registered != &dir_vfs) {
        rc = SQLITE_ERROR;
    } else {
        /* Install the connection callback before publishing process-wide pointers. */
        rc = sqlite3_create_function(db, "dirfd_vfs_root", 1,
                                     SQLITE_UTF8 | SQLITE_DIRECTONLY,
                                     NULL, configure_root, NULL, NULL);
    }
    if (rc == SQLITE_OK && !registered) {
        dir_vfs = (sqlite3_vfs){
            .iVersion = 1, .szOsFile = sizeof(DirFile), .mxPathname = NAME_LIMIT,
            .zName = VFS_NAME, .xOpen = vfs_open, .xDelete = vfs_delete,
            .xAccess = vfs_access, .xFullPathname = vfs_fullpath,
            .xDlOpen = vfs_dlopen, .xDlError = vfs_dlerror, .xDlSym = vfs_dlsym,
            .xDlClose = vfs_dlclose, .xRandomness = vfs_random,
            .xSleep = vfs_sleep, .xCurrentTime = vfs_time, .xGetLastError = vfs_error
        };
        rc = sqlite3_vfs_register(&dir_vfs, 0);
        if (rc != SQLITE_OK)
            sqlite3_create_function(db, "dirfd_vfs_root", 1, SQLITE_UTF8,
                                     NULL, NULL, NULL, NULL);
    }
    sqlite3_mutex_leave(root_mutex);
    return rc == SQLITE_OK ? SQLITE_OK_LOAD_PERMANENTLY : rc;
}
