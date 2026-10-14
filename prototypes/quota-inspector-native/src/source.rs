//! Descriptor-pinned loading of the fixed operator inventory source.
//!
//! Parsing this file creates only a candidate. It does not create a trusted
//! observation or enable the native inspector.

use std::fs::File;
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rustix::fs::{Mode, OFlags, ResolveFlags, fstat, open, openat2};

use crate::{CandidateInventory, Failure, MAX_INVENTORY_BYTES, Unavailable};

const SOURCE_ROOT: &str = "/etc/synai/quota-inspector";
const SOURCE_FILE: &str = "inventory.json";
const SOURCE_UID: u32 = 0;
const SOURCE_GID: u32 = 0;
const SOURCE_DIR_MODE: u32 = 0o755;
const SOURCE_FILE_MODE: u32 = 0o644;
const MAX_ACQUISITION_AGE: Duration = Duration::from_secs(5);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SourceIdentity {
    pub device: u64,
    pub inode: u64,
    pub mode: u32,
    pub uid: u32,
    pub gid: u32,
    pub links: u32,
    pub size: i64,
    pub mtime: i64,
    pub mtime_nsec: u32,
    pub ctime: i64,
    pub ctime_nsec: u32,
}

fn identity(fd: impl AsFd) -> Result<SourceIdentity, Failure> {
    let stat =
        fstat(fd).map_err(|_| Failure::Unavailable(Unavailable::InventoryAuthorityUnavailable))?;
    Ok(SourceIdentity {
        device: stat.st_dev as u64,
        inode: stat.st_ino as u64,
        mode: stat.st_mode,
        uid: stat.st_uid,
        gid: stat.st_gid,
        links: stat.st_nlink as u32,
        size: stat.st_size as i64,
        mtime: stat.st_mtime as i64,
        mtime_nsec: stat.st_mtime_nsec as u32,
        ctime: stat.st_ctime as i64,
        ctime_nsec: stat.st_ctime_nsec as u32,
    })
}

fn same_directory(left: SourceIdentity, right: SourceIdentity) -> bool {
    left.device == right.device
        && left.inode == right.inode
        && left.mode == right.mode
        && left.uid == right.uid
        && left.gid == right.gid
}

fn directory_flags() -> OFlags {
    OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC
}

fn open_root(
    path: &Path,
    expected_uid: u32,
    expected_gid: u32,
    expected_mode: u32,
) -> Result<(OwnedFd, Vec<(PathBuf, SourceIdentity)>), Failure> {
    if !path.is_absolute()
        || path == Path::new("/")
        || path.components().any(|c| {
            matches!(
                c,
                std::path::Component::ParentDir | std::path::Component::CurDir
            )
        })
    {
        return Err(Failure::Unavailable(
            Unavailable::InventoryAuthorityUnavailable,
        ));
    }
    let root = open("/", directory_flags(), Mode::empty())
        .map_err(|_| Failure::Unavailable(Unavailable::InventoryAuthorityUnavailable))?;
    let mut current = root;
    let mut chain = vec![(PathBuf::from("/"), identity(&current)?)];
    let mut current_path = PathBuf::from("/");
    for component in path.components().skip(1) {
        let std::path::Component::Normal(name) = component else {
            return Err(Failure::Unavailable(
                Unavailable::InventoryAuthorityUnavailable,
            ));
        };
        let next = openat2(
            &current,
            name,
            directory_flags() | OFlags::NOFOLLOW,
            Mode::empty(),
            ResolveFlags::BENEATH | ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_MAGICLINKS,
        )
        .map_err(|_| Failure::Unavailable(Unavailable::InventoryAuthorityUnavailable))?;
        let stat = identity(&next)?;
        if stat.mode & 0o170000 != 0o040000 {
            return Err(Failure::Unavailable(
                Unavailable::InventoryAuthorityUnavailable,
            ));
        }
        current_path.push(name);
        let final_component = current_path == path;
        if final_component {
            if stat.uid != expected_uid
                || stat.gid != expected_gid
                || stat.mode & 0o7777 != expected_mode
            {
                return Err(Failure::Unavailable(
                    Unavailable::InventoryAuthorityUnavailable,
                ));
            }
        } else if stat.uid != SOURCE_UID
            || stat.gid != SOURCE_GID
            || stat.mode & 0o022 != 0 && !(stat.mode & 0o1000 != 0 && stat.gid == SOURCE_GID)
        {
            return Err(Failure::Unavailable(
                Unavailable::InventoryAuthorityUnavailable,
            ));
        }
        chain.push((current_path.clone(), stat));
        current = next;
    }
    let final_stat = identity(&current)?;
    if final_stat.uid != expected_uid
        || final_stat.gid != expected_gid
        || final_stat.mode & 0o7777 != expected_mode
    {
        return Err(Failure::Unavailable(
            Unavailable::InventoryAuthorityUnavailable,
        ));
    }
    Ok((current, chain))
}

fn check_source_file(
    fd: impl AsFd,
    expected_uid: u32,
    expected_gid: u32,
    expected_mode: u32,
) -> Result<SourceIdentity, Failure> {
    let stat = identity(fd)?;
    if stat.mode & 0o170000 != 0o100000
        || stat.uid != expected_uid
        || stat.gid != expected_gid
        || stat.mode & 0o7777 != expected_mode
        || stat.links != 1
        || stat.size <= 0
        || stat.size as usize > MAX_INVENTORY_BYTES
    {
        return Err(Failure::Unavailable(
            Unavailable::InventoryAuthorityUnavailable,
        ));
    }
    Ok(stat)
}

fn read_candidate(
    root: &OwnedFd,
    expected_uid: u32,
    expected_gid: u32,
    expected_file_mode: u32,
) -> Result<(CandidateInventory, OwnedFd, SourceIdentity), Failure> {
    let opened = openat2(
        root,
        SOURCE_FILE,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK,
        Mode::empty(),
        ResolveFlags::BENEATH
            | ResolveFlags::NO_SYMLINKS
            | ResolveFlags::NO_MAGICLINKS
            | ResolveFlags::NO_XDEV,
    )
    .map_err(|_| Failure::Unavailable(Unavailable::InventoryAuthorityUnavailable))?;
    let before = check_source_file(&opened, expected_uid, expected_gid, expected_file_mode)?;
    let file = File::from(
        opened
            .try_clone()
            .map_err(|_| Failure::Unavailable(Unavailable::InventoryAuthorityUnavailable))?,
    );
    let mut bytes = vec![0; before.size as usize];
    let mut offset = 0;
    while offset < bytes.len() {
        let read = file
            .read_at(&mut bytes[offset..], offset as u64)
            .map_err(|_| Failure::Unavailable(Unavailable::InventoryAuthorityUnavailable))?;
        if read == 0 {
            return Err(Failure::Unavailable(
                Unavailable::InventoryAuthorityUnavailable,
            ));
        }
        offset += read;
    }
    let after = check_source_file(&opened, expected_uid, expected_gid, expected_file_mode)?;
    if before != after {
        return Err(Failure::Unavailable(
            Unavailable::InventoryAuthorityUnavailable,
        ));
    }
    let rebound = openat2(
        root,
        SOURCE_FILE,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK,
        Mode::empty(),
        ResolveFlags::BENEATH
            | ResolveFlags::NO_SYMLINKS
            | ResolveFlags::NO_MAGICLINKS
            | ResolveFlags::NO_XDEV,
    )
    .map_err(|_| Failure::Unavailable(Unavailable::InventoryAuthorityUnavailable))?;
    if identity(&rebound)? != before {
        return Err(Failure::Unavailable(
            Unavailable::InventoryAuthorityUnavailable,
        ));
    }
    let policy = CandidateInventory::parse(&bytes)?;
    Ok((policy, opened, before))
}

/// A protected-source read and parsed candidate; not a `TrustedInventory`.
pub struct AcquiredInventory {
    policy: CandidateInventory,
    descriptor: OwnedFd,
    source_identity: SourceIdentity,
    acquired_at: Duration,
    acquired_unix_seconds: u64,
}

impl AcquiredInventory {
    fn load(
        root_path: &Path,
        expected_uid: u32,
        expected_gid: u32,
        expected_dir_mode: u32,
        expected_file_mode: u32,
        request_started: Instant,
    ) -> Result<Self, Failure> {
        let now = Instant::now();
        let acquired_at = now
            .checked_duration_since(request_started)
            .ok_or(Failure::Unavailable(Unavailable::StaleEvidence))?;
        if acquired_at > MAX_ACQUISITION_AGE {
            return Err(Failure::Unavailable(Unavailable::StaleEvidence));
        }
        let (root, chain) = open_root(root_path, expected_uid, expected_gid, expected_dir_mode)?;
        let root_stat = identity(&root)?;
        if root_stat.mode & 0o7777 != expected_dir_mode
            || root_stat.uid != expected_uid
            || root_stat.gid != expected_gid
        {
            return Err(Failure::Unavailable(
                Unavailable::InventoryAuthorityUnavailable,
            ));
        }
        let (policy, descriptor, source_identity) =
            read_candidate(&root, expected_uid, expected_gid, expected_file_mode)?;
        let mut current = open("/", directory_flags(), Mode::empty())
            .map_err(|_| Failure::Unavailable(Unavailable::InventoryAuthorityUnavailable))?;
        if !same_directory(identity(&current)?, chain[0].1) {
            return Err(Failure::Unavailable(
                Unavailable::InventoryAuthorityUnavailable,
            ));
        }
        for (path, expected) in chain.iter().skip(1) {
            let name = path.file_name().ok_or(Failure::Unavailable(
                Unavailable::InventoryAuthorityUnavailable,
            ))?;
            current = openat2(
                &current,
                name,
                directory_flags() | OFlags::NOFOLLOW,
                Mode::empty(),
                ResolveFlags::BENEATH | ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_MAGICLINKS,
            )
            .map_err(|_| Failure::Unavailable(Unavailable::InventoryAuthorityUnavailable))?;
            if !same_directory(identity(&current)?, *expected) {
                return Err(Failure::Unavailable(
                    Unavailable::InventoryAuthorityUnavailable,
                ));
            }
        }
        let unix_seconds = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| Failure::Unavailable(Unavailable::StaleEvidence))?
            .as_secs();
        Ok(Self {
            policy,
            descriptor,
            source_identity,
            acquired_at,
            acquired_unix_seconds: unix_seconds,
        })
    }

    /// Reads only the fixed operator source. The path and root identity are not caller-selectable.
    pub fn load_configured(request_started: Instant) -> Result<Self, Failure> {
        Self::load(
            Path::new(SOURCE_ROOT),
            SOURCE_UID,
            SOURCE_GID,
            SOURCE_DIR_MODE,
            SOURCE_FILE_MODE,
            request_started,
        )
    }

    pub fn policy(&self) -> &CandidateInventory {
        &self.policy
    }

    pub fn source_identity(&self) -> SourceIdentity {
        self.source_identity
    }

    pub fn acquired_at(&self) -> Duration {
        self.acquired_at
    }

    pub fn acquired_unix_seconds(&self) -> u64 {
        self.acquired_unix_seconds
    }

    pub fn source_fd(&self) -> BorrowedFd<'_> {
        self.descriptor.as_fd()
    }

    #[cfg(test)]
    fn load_test_root(
        root: &Path,
        expected_uid: u32,
        expected_gid: u32,
        expected_dir_mode: u32,
        expected_file_mode: u32,
        request_started: Instant,
    ) -> Result<Self, Failure> {
        Self::load(
            root,
            expected_uid,
            expected_gid,
            expected_dir_mode,
            expected_file_mode,
            request_started,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::{self, OpenOptions};
    use std::io::Write;
    use std::os::unix::fs::MetadataExt;
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT_DIRECTORY: AtomicU64 = AtomicU64::new(0);

    fn policy_bytes() -> Vec<u8> {
        serde_json::to_vec(&crate::tests::document()).unwrap()
    }

    fn fixture_root(bytes: &[u8], mode: u32) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "synai-inventory-source-{}-{}",
            std::process::id(),
            NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed),
        ));
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(mode)).unwrap();
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(dir.join(SOURCE_FILE))
            .unwrap();
        file.write_all(bytes).unwrap();
        dir
    }

    #[test]
    fn disposable_root_loads_only_a_candidate_and_keeps_source_fd_pinned() {
        let root = fixture_root(&policy_bytes(), 0o700);
        let result = AcquiredInventory::load_test_root(
            &root,
            fs::metadata(&root).unwrap().uid(),
            fs::metadata(&root).unwrap().gid(),
            0o700,
            0o600,
            Instant::now(),
        );
        let acquired = result.unwrap();
        assert_eq!(acquired.policy.role_count(), 10);
        assert_eq!(acquired.source_identity.links, 1);
        let current = identity(acquired.source_fd()).unwrap();
        assert_eq!(current.device, acquired.source_identity.device);
        assert_eq!(current.inode, acquired.source_identity.inode);
        assert!(acquired.acquired_unix_seconds > 0);
        assert!(!acquired.policy.policy_digest().is_empty());
        drop(acquired);
        fs::remove_file(root.join(SOURCE_FILE)).unwrap();
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn test_loader_rejects_symlinked_source() {
        let root = fixture_root(b"{}", 0o700);
        let path = root.join(SOURCE_FILE);
        let replacement = root.join("replacement");
        fs::rename(&path, &replacement).unwrap();
        std::os::unix::fs::symlink(&replacement, &path).unwrap();
        let result = AcquiredInventory::load_test_root(
            &root,
            fs::metadata(&root).unwrap().uid(),
            fs::metadata(&root).unwrap().gid(),
            0o700,
            0o600,
            Instant::now(),
        );
        assert!(result.is_err());
        fs::remove_file(path).unwrap();
        fs::remove_file(replacement).unwrap();
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn test_loader_rejects_wrong_permissions_hardlinks_and_oversize() {
        let root = fixture_root(&policy_bytes(), 0o700);
        let source = root.join(SOURCE_FILE);
        fs::set_permissions(&source, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(
            AcquiredInventory::load_test_root(
                &root,
                fs::metadata(&root).unwrap().uid(),
                fs::metadata(&root).unwrap().gid(),
                0o700,
                0o600,
                Instant::now(),
            )
            .is_err()
        );
        fs::set_permissions(&source, fs::Permissions::from_mode(0o600)).unwrap();
        let second = root.join("hardlink");
        fs::hard_link(&source, &second).unwrap();
        assert!(
            AcquiredInventory::load_test_root(
                &root,
                fs::metadata(&root).unwrap().uid(),
                fs::metadata(&root).unwrap().gid(),
                0o700,
                0o600,
                Instant::now(),
            )
            .is_err()
        );
        fs::remove_file(second).unwrap();
        fs::write(&source, vec![b'x'; MAX_INVENTORY_BYTES + 1]).unwrap();
        assert!(
            AcquiredInventory::load_test_root(
                &root,
                fs::metadata(&root).unwrap().uid(),
                fs::metadata(&root).unwrap().gid(),
                0o700,
                0o600,
                Instant::now(),
            )
            .is_err()
        );
        fs::remove_file(source).unwrap();
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn test_loader_rejects_stale_request_and_symlinked_root() {
        let root = fixture_root(&policy_bytes(), 0o700);
        let uid = fs::metadata(&root).unwrap().uid();
        let gid = fs::metadata(&root).unwrap().gid();
        assert!(
            AcquiredInventory::load_test_root(
                &root,
                uid,
                gid,
                0o700,
                0o600,
                Instant::now() - Duration::from_secs(6),
            )
            .is_err()
        );
        let alias = root.with_extension("alias");
        std::os::unix::fs::symlink(&root, &alias).unwrap();
        assert!(
            AcquiredInventory::load_test_root(&alias, uid, gid, 0o700, 0o600, Instant::now(),)
                .is_err()
        );
        fs::remove_file(alias).unwrap();
        fs::remove_file(root.join(SOURCE_FILE)).unwrap();
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn acquired_policy_and_descriptor_remain_bound_after_path_replacement() {
        let root = fixture_root(&policy_bytes(), 0o700);
        let acquired = AcquiredInventory::load_test_root(
            &root,
            fs::metadata(&root).unwrap().uid(),
            fs::metadata(&root).unwrap().gid(),
            0o700,
            0o600,
            Instant::now(),
        )
        .unwrap();
        let original_generation = acquired.policy.generation().to_owned();
        let source = root.join(SOURCE_FILE);
        let moved = root.join("old-inventory");
        fs::rename(&source, &moved).unwrap();
        fs::write(&source, b"replacement").unwrap();
        assert_eq!(acquired.policy.generation(), original_generation);
        let current = identity(acquired.source_fd()).unwrap();
        assert_eq!(current.device, acquired.source_identity.device);
        assert_eq!(current.inode, acquired.source_identity.inode);
        drop(acquired);
        fs::remove_file(source).unwrap();
        fs::remove_file(moved).unwrap();
        fs::remove_dir(root).unwrap();
    }
}
