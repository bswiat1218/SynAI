/* Compile-only assertions; no function, executable, or quota call. */
#include <stddef.h>
#include <stdint.h>
#include <limits.h>
#include <errno.h>
#include <linux/quota.h>
#include <asm/unistd.h>

#if !defined(__linux__) || !defined(__x86_64__) || !defined(__LP64__) || !defined(__GLIBC__)
#error Unsupported ABI target
#endif
_Static_assert(CHAR_BIT == 8 && sizeof(long) == 8 && sizeof(void *) == 8, "LP64");
_Static_assert(sizeof(unsigned int) == 4, "command and ID width");
_Static_assert((unsigned int)-1 > 0, "unsigned arguments");
_Static_assert((__u64)-1 > 0 && (__u32)-1 > 0, "unsigned record fields");
_Static_assert(sizeof(__kernel_uid32_t) == 4, "kernel quota ID width");
_Static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__, "byte order");
_Static_assert(__NR_quotactl_fd == 443, "syscall identity only");
_Static_assert(PRJQUOTA == 2 && SUBCMDSHIFT == 8 && SUBCMDMASK == 255, "encoding");
_Static_assert((((uint32_t)Q_GETQUOTA * 256U) | PRJQUOTA) == 0x80000702U, "GETQUOTA");
_Static_assert((((uint32_t)Q_GETINFO * 256U) | PRJQUOTA) == 0x80000502U, "GETINFO");
_Static_assert(QIF_DQBLKSIZE == 1024, "limit units");
_Static_assert(QIF_BLIMITS == 1 && QIF_SPACE == 2 && QIF_ILIMITS == 4 &&
               QIF_INODES == 8 && QIF_BTIME == 16 && QIF_ITIME == 32, "quota bits");
_Static_assert(QIF_ALL == 63 && IIF_ALL == 7, "validity");
_Static_assert(IIF_BGRACE == 1 && IIF_IGRACE == 2 && IIF_FLAGS == 4, "info bits");
_Static_assert(DQF_ROOT_SQUASH == 1 && DQF_SYS_FILE == 65536, "info flags");
_Static_assert(sizeof(struct if_dqblk) == 72 && _Alignof(struct if_dqblk) == 8, "dqblk");
_Static_assert(sizeof(struct if_dqinfo) == 24 && _Alignof(struct if_dqinfo) == 8, "dqinfo");
#define FIELD(type, field, offset, width) \
    _Static_assert(offsetof(struct type, field) == offset && \
                   sizeof(((struct type *)0)->field) == width, #field)
FIELD(if_dqblk, dqb_bhardlimit, 0, 8);
FIELD(if_dqblk, dqb_bsoftlimit, 8, 8);
FIELD(if_dqblk, dqb_curspace, 16, 8);
FIELD(if_dqblk, dqb_ihardlimit, 24, 8);
FIELD(if_dqblk, dqb_isoftlimit, 32, 8);
FIELD(if_dqblk, dqb_curinodes, 40, 8);
FIELD(if_dqblk, dqb_btime, 48, 8);
FIELD(if_dqblk, dqb_itime, 56, 8);
FIELD(if_dqblk, dqb_valid, 64, 4);
FIELD(if_dqinfo, dqi_bgrace, 0, 8);
FIELD(if_dqinfo, dqi_igrace, 8, 8);
FIELD(if_dqinfo, dqi_flags, 16, 4);
FIELD(if_dqinfo, dqi_valid, 20, 4);
_Static_assert(ENOSYS == 38 && EOPNOTSUPP == 95 && ENOTSUP == EOPNOTSUPP, "unsupported");
_Static_assert(EPERM == 1 && EACCES == 13 && EBADF == 9 && EINVAL == 22, "arguments");
_Static_assert(ENOENT == 2 && ESRCH == 3 && ENODATA == 61 && EINTR == 4, "records");
_Static_assert(EIO == 5 && EFAULT == 14 && ECANCELED == 125 && ETIMEDOUT == 110, "errors");
