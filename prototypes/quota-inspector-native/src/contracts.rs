//! Pure validation of untrusted acquisition/handoff candidates. No result grants authority.

use crate::{
    CandidateInventory, Failure, Inconsistent, MAX_INVENTORY_BYTES, NamespaceId, NodeEvidence,
    NodeType, SampleTime, Unavailable,
};

fn identity(expected: &NodeEvidence, observed: &NodeEvidence) -> Result<(), Failure> {
    if expected.inode == 0 || observed.inode == 0 || expected.filesystem.is_none() {
        return Err(Failure::Unavailable(Unavailable::MissingEvidence));
    }
    let filesystem = expected.filesystem.as_ref().expect("checked filesystem");
    if filesystem.namespace.inode == 0 || filesystem.mount_id == 0 {
        return Err(Failure::Unavailable(Unavailable::NamespaceUnavailable));
    }
    if expected != observed {
        return Err(Failure::Inconsistent(Inconsistent::ChangedEvidence));
    }
    Ok(())
}

fn freshness(started: SampleTime, acquired: SampleTime, now: SampleTime) -> Result<(), Failure> {
    if acquired.monotonic < started.monotonic
        || acquired.monotonic > now.monotonic
        || now.monotonic < started.monotonic
        || acquired.unix_seconds > now.unix_seconds
        || now.unix_seconds.saturating_sub(acquired.unix_seconds) > 5
    {
        return Err(Failure::Unavailable(Unavailable::StaleEvidence));
    }
    Ok(())
}

/// The expected source must eventually come from independently authenticated policy.
/// Caller-provided matching metadata and digests do not authenticate acquisition.
#[derive(Debug, Clone)]
pub struct InventoryAcquisitionCandidate {
    pub before: NodeEvidence,
    pub after: NodeEvidence,
    pub transferred_bytes: usize,
    pub policy_digest: String,
    pub generation: String,
    pub acquired_at: SampleTime,
}

impl InventoryAcquisitionCandidate {
    pub fn check(
        &self,
        policy: &CandidateInventory,
        expected_source: &NodeEvidence,
        started: SampleTime,
        now: SampleTime,
    ) -> Result<(), Failure> {
        if self.transferred_bytes == 0 || self.transferred_bytes > MAX_INVENTORY_BYTES {
            return Err(Failure::Unavailable(Unavailable::ResourceLimit));
        }
        if self.policy_digest != policy.policy_digest() || self.generation != policy.generation() {
            return Err(Failure::Inconsistent(Inconsistent::InventoryBinding));
        }
        if self.before.node_type != NodeType::RegularFile
            || self.before.links != 1
            || !matches!(self.before.mode, 0o400 | 0o600)
        {
            return Err(Failure::Inconsistent(Inconsistent::Ownership));
        }
        identity(expected_source, &self.before)?;
        identity(&self.before, &self.after)?;
        freshness(started, self.acquired_at, now)
    }
}

/// Same-namespace handoff model. Cross-namespace mount IDs cannot be compared directly.
#[derive(Debug, Clone)]
pub struct MountedSourceCandidate {
    pub before: NodeEvidence,
    pub mounted: NodeEvidence,
    pub after: NodeEvidence,
    pub consumer_namespace: NamespaceId,
}

impl MountedSourceCandidate {
    pub fn check(&self, pinned_source: &NodeEvidence) -> Result<(), Failure> {
        let filesystem = pinned_source
            .filesystem
            .as_ref()
            .ok_or(Failure::Unavailable(Unavailable::MissingEvidence))?;
        if self.consumer_namespace.inode == 0 || filesystem.namespace.inode == 0 {
            return Err(Failure::Unavailable(Unavailable::NamespaceUnavailable));
        }
        if self.consumer_namespace != filesystem.namespace {
            return Err(Failure::Unavailable(Unavailable::NamespaceUnavailable));
        }
        if pinned_source.node_type != NodeType::Directory {
            return Err(Failure::Inconsistent(Inconsistent::NodeIdentity));
        }
        identity(pinned_source, &self.before)?;
        identity(pinned_source, &self.mounted)?;
        identity(pinned_source, &self.after)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VfsFileKind {
    Database,
    Wal,
    Shm,
    Journal,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VfsOperation {
    Open,
    Create,
    Read,
    Write,
    Truncate,
    Lock,
    Sync,
    Checkpoint,
    Close,
}

/// An individual future VFS operation's FD identity contract, not a SQLite VFS implementation.
#[derive(Debug, Clone)]
pub struct VfsFileCandidate {
    pub kind: VfsFileKind,
    pub operation: VfsOperation,
    pub before: NodeEvidence,
    pub after: NodeEvidence,
}

impl VfsFileCandidate {
    pub fn check(
        &self,
        expected_kind: VfsFileKind,
        expected_operation: VfsOperation,
        pinned_file: &NodeEvidence,
    ) -> Result<(), Failure> {
        if self.kind != expected_kind || self.operation != expected_operation {
            return Err(Failure::Inconsistent(Inconsistent::NodeIdentity));
        }
        if pinned_file.node_type != NodeType::RegularFile
            || pinned_file.links != 1
            || pinned_file.mode != 0o600
            || pinned_file.project.is_none()
        {
            return Err(Failure::Inconsistent(Inconsistent::NodeIdentity));
        }
        if pinned_file
            .project
            .is_some_and(|project| project.project_id.0 == 0)
        {
            return Err(Failure::Inconsistent(Inconsistent::ProjectIdentity));
        }
        identity(pinned_file, &self.before)?;
        identity(pinned_file, &self.after)
    }
}

/// Claimed effective confinement is untrusted until independently acquired and qualified.
#[derive(Debug, Clone, Copy)]
pub struct ConfinementCandidate {
    pub broker_effective_capabilities: u64,
    pub dedicated_identity: bool,
    pub no_supplementary_groups: bool,
    pub no_new_privileges: bool,
    pub seccomp_enforcing: bool,
    pub lsm_enforcing: bool,
}

impl ConfinementCandidate {
    pub fn check(&self) -> Result<(), Failure> {
        if self.broker_effective_capabilities != 0
            || !self.dedicated_identity
            || !self.no_supplementary_groups
            || !self.no_new_privileges
            || !self.seccomp_enforcing
            || !self.lsm_enforcing
        {
            return Err(Failure::Unavailable(Unavailable::PermissionDenied));
        }
        Ok(())
    }
}

pub(crate) fn check_acquisition_time(
    started: SampleTime,
    acquired: SampleTime,
    now: SampleTime,
) -> Result<(), Failure> {
    freshness(started, acquired, now)
}
