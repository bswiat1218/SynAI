#![forbid(unsafe_code)]

//! Untrusted handoff candidates cannot be promoted to a validated observation:
//! ```compile_fail
//! use quota_inspector_native::{MountedSourceCandidate, ValidatedObservation};
//! fn promote(candidate: MountedSourceCandidate) -> ValidatedObservation {
//!     candidate.into()
//! }
//! ```
//! ```compile_fail
//! use quota_inspector_native::{InventoryAcquisitionCandidate, TrustedInventory};
//! fn promote(candidate: InventoryAcquisitionCandidate) -> TrustedInventory {
//!     candidate.into()
//! }
//! ```
//! ```compile_fail
//! use quota_inspector_native::supervision::WorkerLease;
//! ```

mod contracts;
mod evidence;
mod inventory;
mod observation;
pub mod source;
#[cfg(test)]
mod supervision;

pub use contracts::{
    ConfinementCandidate, InventoryAcquisitionCandidate, MountedSourceCandidate, VfsFileCandidate,
    VfsFileKind, VfsOperation,
};

pub use evidence::{
    Capacity, HardLimits, NodeEvidence, NodeType, PinnedDescriptor, ProjectAttributes, ProjectId,
    QuotaEvidence, QuotaReading, QuotaState, ReadOnlyQuotaFields, SampleTime, TrustedInventory,
    Usage,
};
pub use inventory::{
    CONTINUOUS_RESERVE, CandidateInventory, DeviceId, FilesystemIdentity, HARD_ENTITLEMENTS,
    HARD_INODE_ENTITLEMENTS, INITIAL_AVAILABLE_BYTES, INITIAL_AVAILABLE_INODES, MAX_COMPONENTS,
    MAX_EXISTING_FILES, MAX_INVENTORY_BYTES, MAX_PLANNED_STORAGE, MAX_ROLES, NamespaceId,
    OPERATIONAL_ALLOWANCE, PersistentPurpose, Purpose,
};
pub use observation::{
    BoundRequest, InspectionResult, MAX_DESCRIPTORS, MAX_OPERATION_TIME, NativeInspector,
    ObservationBudget, Outcome, ValidatedObservation,
};
pub use source::{AcquiredInventory, SourceIdentity};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Unavailable {
    InventoryInvalid,
    InventoryAuthorityUnavailable,
    NativeBackendUnavailable,
    UnsupportedInterface,
    PermissionDenied,
    MissingEvidence,
    NamespaceUnavailable,
    DescriptorUnavailable,
    MalformedEvidence,
    ArithmeticOverflow,
    StaleEvidence,
    RequestInvalid,
    DeadlineExceeded,
    Cancelled,
    Blocked,
    UnresolvedOwnership,
    Busy,
    ResourceLimit,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Inconsistent {
    AliasedDomain,
    InventoryBinding,
    FilesystemIdentity,
    NamespaceMismatch,
    NodeIdentity,
    Ownership,
    ProjectIdentity,
    Inheritance,
    HardLinks,
    QuotaNotEnforced,
    HardLimits,
    Usage,
    ChangedEvidence,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Failure {
    Unavailable(Unavailable),
    Inconsistent(Inconsistent),
}

impl Failure {
    pub fn code(self) -> &'static str {
        match self {
            Self::Unavailable(reason) => match reason {
                Unavailable::InventoryInvalid => "inventory_invalid",
                Unavailable::InventoryAuthorityUnavailable => "inventory_authority_unavailable",
                Unavailable::NativeBackendUnavailable => "native_backend_unavailable",
                Unavailable::UnsupportedInterface => "unsupported_interface",
                Unavailable::PermissionDenied => "permission_denied",
                Unavailable::MissingEvidence => "missing_evidence",
                Unavailable::NamespaceUnavailable => "namespace_unavailable",
                Unavailable::DescriptorUnavailable => "descriptor_unavailable",
                Unavailable::MalformedEvidence => "malformed_evidence",
                Unavailable::ArithmeticOverflow => "arithmetic_overflow",
                Unavailable::StaleEvidence => "stale_evidence",
                Unavailable::RequestInvalid => "request_invalid",
                Unavailable::DeadlineExceeded => "deadline_exceeded",
                Unavailable::Cancelled => "cancelled",
                Unavailable::Blocked => "blocked",
                Unavailable::UnresolvedOwnership => "unresolved_ownership",
                Unavailable::Busy => "busy",
                Unavailable::ResourceLimit => "resource_limit",
            },
            Self::Inconsistent(reason) => match reason {
                Inconsistent::AliasedDomain => "aliased_domain",
                Inconsistent::InventoryBinding => "inventory_binding",
                Inconsistent::FilesystemIdentity => "filesystem_identity",
                Inconsistent::NamespaceMismatch => "namespace_mismatch",
                Inconsistent::NodeIdentity => "node_identity",
                Inconsistent::Ownership => "ownership",
                Inconsistent::ProjectIdentity => "project_identity",
                Inconsistent::Inheritance => "inheritance",
                Inconsistent::HardLinks => "hard_links",
                Inconsistent::QuotaNotEnforced => "quota_not_enforced",
                Inconsistent::HardLimits => "hard_limits",
                Inconsistent::Usage => "usage",
                Inconsistent::ChangedEvidence => "changed_evidence",
            },
        }
    }
}

#[cfg(test)]
mod contract_tests;
#[cfg(test)]
mod tests;
