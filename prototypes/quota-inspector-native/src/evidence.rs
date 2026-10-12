use std::os::fd::{AsFd, BorrowedFd, OwnedFd};
use std::sync::Arc;
use std::time::Duration;

use crate::inventory::{CandidateInventory, FilesystemIdentity, NamespaceId};
use crate::{Failure, Unavailable};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProjectId(pub(crate) u32);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HardLimits {
    pub bytes: u64,
    pub inodes: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Usage {
    pub bytes: u64,
    pub inodes: u64,
}

/// Untrusted field-level evidence, not an ABI buffer and not an observation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReadOnlyQuotaFields {
    pub valid_mask: u32,
    pub hard_blocks_1024: Option<u64>,
    pub hard_inodes: Option<u64>,
    pub used_bytes: Option<u64>,
    pub used_inodes: Option<u64>,
}

impl ReadOnlyQuotaFields {
    pub fn validated_counters(self) -> Result<(HardLimits, Usage), Failure> {
        if self.valid_mask & 0x0f != 0x0f || self.valid_mask & !0x3f != 0 {
            return Err(Failure::Unavailable(Unavailable::MissingEvidence));
        }
        let missing = Failure::Unavailable(Unavailable::MissingEvidence);
        let bytes = self
            .hard_blocks_1024
            .ok_or(missing)?
            .checked_mul(1024)
            .ok_or(Failure::Unavailable(Unavailable::ArithmeticOverflow))?;
        let limits = HardLimits {
            bytes,
            inodes: self.hard_inodes.ok_or(missing)?,
        };
        let usage = Usage {
            bytes: self.used_bytes.ok_or(missing)?,
            inodes: self.used_inodes.ok_or(missing)?,
        };
        if limits.bytes == 0
            || limits.inodes == 0
            || usage.bytes > limits.bytes
            || usage.inodes > limits.inodes
        {
            return Err(Failure::Inconsistent(crate::Inconsistent::Usage));
        }
        Ok((limits, usage))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QuotaState {
    Disabled,
    AccountingOnly,
    Enforced,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeType {
    Directory,
    RegularFile,
    Unsupported,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProjectAttributes {
    pub project_id: ProjectId,
    pub inheritance: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NodeEvidence {
    pub inode: u64,
    pub links: u64,
    pub node_type: NodeType,
    pub owner_uid: u32,
    pub owner_gid: u32,
    pub mode: u32,
    pub filesystem: Option<FilesystemIdentity>,
    pub project: Option<ProjectAttributes>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Capacity {
    pub blocks: u64,
    pub available_blocks: u64,
    pub fragment_bytes: u64,
    pub inodes: u64,
    pub available_inodes: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SampleTime {
    pub monotonic: Duration,
    pub unix_seconds: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuotaReading {
    pub project_id: ProjectId,
    pub filesystem: FilesystemIdentity,
    pub state: QuotaState,
    pub limits: HardLimits,
    pub usage: Usage,
    pub capacity: Capacity,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuotaEvidence {
    pub before: QuotaReading,
    pub after: QuotaReading,
    pub sampled_at: SampleTime,
}

/// A digest is binding only; this attestation requires separately accepted acquisition.
pub(crate) struct InventoryAttestation {
    pub policy_digest: String,
    pub generation: String,
    pub approval_identity: String,
    pub consumer_namespace: Option<NamespaceId>,
}

pub struct TrustedInventory {
    pub(crate) policy: CandidateInventory,
    pub(crate) attestation: InventoryAttestation,
}

impl TrustedInventory {
    pub fn generation(&self) -> &str {
        self.policy.generation()
    }

    pub fn approval_identity(&self) -> &str {
        &self.attestation.approval_identity
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ResolutionRules {
    pub beneath: bool,
    pub no_symlinks: bool,
    pub no_magiclinks: bool,
    pub no_mount_crossing: bool,
    pub read_only: bool,
}

pub(crate) const RESOLUTION: ResolutionRules = ResolutionRules {
    beneath: true,
    no_symlinks: true,
    no_magiclinks: true,
    no_mount_crossing: true,
    read_only: true,
};

pub struct PinnedDescriptor {
    pub(crate) descriptor: OwnedFd,
    pub(crate) initial: NodeEvidence,
    pub(crate) edge: Option<(usize, String)>,
}

impl PinnedDescriptor {
    pub(crate) fn fd(&self) -> BorrowedFd<'_> {
        self.descriptor.as_fd()
    }
}

pub(crate) struct DescriptorSet {
    pub nodes: Vec<PinnedDescriptor>,
    pub role_index: usize,
    pub file_indices: Vec<usize>,
}

#[cfg_attr(
    not(test),
    expect(
        dead_code,
        reason = "No qualified native adapter may produce evidence in this phase"
    )
)]
pub(crate) enum QueryProgress {
    // The backend has finished using the lease; unresolved work or cleanup stays Pending.
    Complete(Result<Box<QuotaEvidence>, Failure>),
    Pending(Unavailable),
}

/// Sealed, read-only contract. No external adapter or raw command dispatch is exposed.
pub(crate) trait ReadOnlyBackend {
    fn clock(&self) -> SampleTime;
    fn attest(&mut self, policy: &CandidateInventory) -> Result<InventoryAttestation, Failure>;
    fn trusted_root(&mut self, inventory: &TrustedInventory) -> Result<OwnedFd, Failure>;
    fn root_still_attested(
        &mut self,
        inventory: &TrustedInventory,
        root: BorrowedFd<'_>,
    ) -> Result<(), Failure>;
    fn open_component(
        &mut self,
        parent: BorrowedFd<'_>,
        name: &str,
        rules: ResolutionRules,
    ) -> Result<OwnedFd, Failure>;
    fn node(&mut self, descriptor: BorrowedFd<'_>) -> Result<NodeEvidence, Failure>;
    fn same_entry(
        &mut self,
        parent: BorrowedFd<'_>,
        name: &str,
        child: BorrowedFd<'_>,
        rules: ResolutionRules,
    ) -> Result<(), Failure>;
    // Only the already validated root descriptor and inventory-bound project are queryable.
    // Any asynchronous implementation must independently retain this lease until completion.
    fn read_project_quota(
        &mut self,
        filesystem: BorrowedFd<'_>,
        project: ProjectId,
        lease: Arc<DescriptorSet>,
    ) -> QueryProgress;
    fn poll_quota(&mut self) -> QueryProgress;
}

pub(crate) struct UnavailableBackend;

impl ReadOnlyBackend for UnavailableBackend {
    fn clock(&self) -> SampleTime {
        // No live acquisition occurs; the failure does not depend on this sample.
        SampleTime {
            monotonic: Duration::ZERO,
            unix_seconds: 0,
        }
    }

    fn attest(&mut self, _: &CandidateInventory) -> Result<InventoryAttestation, Failure> {
        Err(Failure::Unavailable(
            Unavailable::InventoryAuthorityUnavailable,
        ))
    }

    fn trusted_root(&mut self, _: &TrustedInventory) -> Result<OwnedFd, Failure> {
        Err(Failure::Unavailable(Unavailable::NativeBackendUnavailable))
    }

    fn root_still_attested(
        &mut self,
        _: &TrustedInventory,
        _: BorrowedFd<'_>,
    ) -> Result<(), Failure> {
        Err(Failure::Unavailable(Unavailable::NativeBackendUnavailable))
    }

    fn open_component(
        &mut self,
        _: BorrowedFd<'_>,
        _: &str,
        _: ResolutionRules,
    ) -> Result<OwnedFd, Failure> {
        Err(Failure::Unavailable(Unavailable::NativeBackendUnavailable))
    }

    fn node(&mut self, _: BorrowedFd<'_>) -> Result<NodeEvidence, Failure> {
        Err(Failure::Unavailable(Unavailable::NativeBackendUnavailable))
    }

    fn same_entry(
        &mut self,
        _: BorrowedFd<'_>,
        _: &str,
        _: BorrowedFd<'_>,
        _: ResolutionRules,
    ) -> Result<(), Failure> {
        Err(Failure::Unavailable(Unavailable::NativeBackendUnavailable))
    }

    fn read_project_quota(
        &mut self,
        _: BorrowedFd<'_>,
        _: ProjectId,
        _: Arc<DescriptorSet>,
    ) -> QueryProgress {
        QueryProgress::Complete(Err(Failure::Unavailable(
            Unavailable::NativeBackendUnavailable,
        )))
    }

    fn poll_quota(&mut self) -> QueryProgress {
        QueryProgress::Complete(Err(Failure::Unavailable(
            Unavailable::NativeBackendUnavailable,
        )))
    }
}
