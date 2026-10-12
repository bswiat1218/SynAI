use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};

use crate::evidence::{
    DescriptorSet, InventoryAttestation, QueryProgress, RESOLUTION, ReadOnlyBackend,
    UnavailableBackend,
};
use crate::inventory::{RoleDefinition, hex};
use crate::{
    CandidateInventory, Failure, HardLimits, Inconsistent, MAX_COMPONENTS, MAX_EXISTING_FILES,
    NodeEvidence, NodeType, PinnedDescriptor, ProjectId, QuotaEvidence, QuotaState, SampleTime,
    TrustedInventory, Unavailable, Usage,
};

pub const MAX_DESCRIPTORS: usize = 1 + MAX_COMPONENTS + MAX_EXISTING_FILES;
pub const MAX_OPERATION_TIME: Duration = Duration::from_millis(500);
const MAX_AGE_SECONDS: u64 = 5;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    schema_version: u8,
    message_type: String,
    role_id: String,
    challenge: String,
    inventory_generation: String,
}

#[derive(Debug, Clone)]
pub struct BoundRequest(Request);

impl BoundRequest {
    pub fn parse(policy: &CandidateInventory, payload: &[u8]) -> Result<Self, Failure> {
        let invalid = Failure::Unavailable(Unavailable::RequestInvalid);
        if payload.is_empty() || payload.len() > 16_384 {
            return Err(invalid);
        }
        let request: Request = serde_json::from_slice(payload).map_err(|_| invalid)?;
        if request.schema_version != 1
            || request.message_type != "inspect_storage_role"
            || !hex(&request.challenge, 64)
            || request.inventory_generation != policy.generation()
        {
            return Err(invalid);
        }
        policy.role(&request.role_id)?;
        Ok(Self(request))
    }
}

#[derive(Debug, Clone, Copy)]
pub struct ObservationBudget(Duration);

impl ObservationBudget {
    pub fn new(duration: Duration) -> Result<Self, Failure> {
        if duration.is_zero() || duration > MAX_OPERATION_TIME {
            return Err(Failure::Unavailable(Unavailable::ResourceLimit));
        }
        Ok(Self(duration))
    }
}

/// No public constructor or deserializer: only the evidence pipeline can issue this.
///
/// ```compile_fail
/// use quota_inspector_native::ValidatedObservation;
/// let observation = ValidatedObservation { observed_at: 0 };
/// ```
#[derive(Debug, Clone)]
pub struct ValidatedObservation {
    filesystem_uuid: String,
    mount_target: String,
    mount_id: u64,
    project_id: ProjectId,
    limits: HardLimits,
    usage: Usage,
    filesystem_bytes: u64,
    filesystem_bytes_available: u64,
    filesystem_inodes: u64,
    filesystem_inodes_available: u64,
    owner_uid: u32,
    owner_gid: u32,
    observed_at: u64,
}

#[derive(Debug, Clone)]
pub enum Outcome {
    Observed(ValidatedObservation),
    Failed(Failure),
}

#[derive(Debug, Clone)]
pub struct InspectionResult {
    binding: BoundRequest,
    outcome: Outcome,
}

#[derive(Serialize)]
struct Common<'a> {
    schema_version: u8,
    message_type: &'static str,
    role_id: &'a str,
    challenge: &'a str,
    inventory_generation: &'a str,
    helper_boot_epoch: &'a str,
}

#[derive(Serialize)]
struct ErrorEnvelope<'a> {
    #[serde(flatten)]
    common: Common<'a>,
    status: &'static str,
    error_code: &'static str,
}

#[derive(Serialize)]
struct ObservationEnvelope<'a> {
    #[serde(flatten)]
    common: Common<'a>,
    status: &'static str,
    observed_at: u64,
    filesystem_uuid: &'a str,
    mount_target: &'a str,
    mount_id: u64,
    filesystem_type: &'static str,
    quota_mode: &'static str,
    quota_configuration_enabled: bool,
    project_id: u32,
    inheritance_enabled: bool,
    byte_limit: u64,
    inode_limit: u64,
    bytes_used: u64,
    inodes_used: u64,
    filesystem_bytes: u64,
    filesystem_bytes_available: u64,
    filesystem_inodes: u64,
    filesystem_inodes_available: u64,
    owner_uid: u32,
    owner_gid: u32,
}

impl InspectionResult {
    #[cfg(test)]
    pub(crate) fn from_test_observation(
        binding: BoundRequest,
        observation: ValidatedObservation,
    ) -> Self {
        Self {
            binding,
            outcome: Outcome::Observed(observation),
        }
    }

    pub fn outcome(&self) -> &Outcome {
        &self.outcome
    }

    pub fn protocol_response(&self, epoch: &str) -> Result<Vec<u8>, Failure> {
        let invalid = Failure::Unavailable(Unavailable::RequestInvalid);
        if !hex(epoch, 32) {
            return Err(invalid);
        }
        let common = Common {
            schema_version: 1,
            message_type: "storage_inspection_result",
            role_id: &self.binding.0.role_id,
            challenge: &self.binding.0.challenge,
            inventory_generation: &self.binding.0.inventory_generation,
            helper_boot_epoch: epoch,
        };
        let bytes = match &self.outcome {
            Outcome::Failed(error) => serde_json::to_vec(&ErrorEnvelope {
                common,
                status: match error {
                    Failure::Unavailable(_) => "unavailable",
                    Failure::Inconsistent(_) => "inconsistent",
                },
                error_code: error.code(),
            }),
            Outcome::Observed(observation) => serde_json::to_vec(&ObservationEnvelope {
                common,
                status: "ok",
                observed_at: observation.observed_at,
                filesystem_uuid: &observation.filesystem_uuid,
                mount_target: &observation.mount_target,
                mount_id: observation.mount_id,
                filesystem_type: "ext4",
                quota_mode: "project",
                quota_configuration_enabled: true,
                project_id: observation.project_id.0,
                inheritance_enabled: true,
                byte_limit: observation.limits.bytes,
                inode_limit: observation.limits.inodes,
                bytes_used: observation.usage.bytes,
                inodes_used: observation.usage.inodes,
                filesystem_bytes: observation.filesystem_bytes,
                filesystem_bytes_available: observation.filesystem_bytes_available,
                filesystem_inodes: observation.filesystem_inodes,
                filesystem_inodes_available: observation.filesystem_inodes_available,
                owner_uid: observation.owner_uid,
                owner_gid: observation.owner_gid,
            }),
        }
        .map_err(|_| Failure::Unavailable(Unavailable::MalformedEvidence))?;
        if bytes.len() > 16_384 {
            return Err(Failure::Unavailable(Unavailable::ResourceLimit));
        }
        Ok(bytes)
    }
}

/// One owned operation, no listener, worker queue, fixture selector, or kernel calls.
///
/// Test adapters are not exported or compiled into this library artifact:
///
/// ```compile_fail
/// use quota_inspector_native::tests::FakeBackend;
/// ```
///
/// There is no fixture API in the native dependency closure:
///
/// ```compile_fail
/// use quota_inspector_native::{Inventory, Backend};
/// ```
///
/// External callers cannot inject a synthetic backend:
///
/// ```compile_fail
/// use quota_inspector_native::NativeInspector;
/// let inspector = NativeInspector::with_backend("fixture");
/// ```
///
/// Parsed policy cannot be promoted to trusted inventory by a caller:
///
/// ```compile_fail
/// use quota_inspector_native::TrustedInventory;
/// let inventory = TrustedInventory {};
/// ```
pub struct NativeInspector {
    controller: Controller<UnavailableBackend>,
}

impl Default for NativeInspector {
    fn default() -> Self {
        Self::new()
    }
}

impl NativeInspector {
    pub fn new() -> Self {
        Self {
            controller: Controller { active: None },
        }
    }

    pub fn inspect(
        &mut self,
        policy: &CandidateInventory,
        binding: &BoundRequest,
        budget: ObservationBudget,
    ) -> InspectionResult {
        self.controller
            .inspect(policy, binding, budget, UnavailableBackend)
    }

    pub fn cancel(&mut self) {
        self.controller.cancel();
    }

    pub fn poll(&mut self) -> Option<InspectionResult> {
        self.controller.poll()
    }
}

pub(crate) struct Controller<B: ReadOnlyBackend> {
    pub active: Option<Inspection<B>>,
}

impl<B: ReadOnlyBackend> Controller<B> {
    pub(crate) fn inspect(
        &mut self,
        policy: &CandidateInventory,
        binding: &BoundRequest,
        budget: ObservationBudget,
        backend: B,
    ) -> InspectionResult {
        if self.active.is_some() {
            return result(binding, Err(Failure::Unavailable(Unavailable::Busy)));
        }
        let operation = Inspection::begin(policy, binding, budget, backend);
        let mut operation = match operation {
            Ok(operation) => operation,
            Err(error) => return result(binding, Err(error)),
        };
        let outcome = operation.poll();
        if operation.pending() {
            self.active = Some(operation);
        }
        result(binding, outcome)
    }

    pub(crate) fn cancel(&mut self) {
        if let Some(operation) = self.active.as_mut() {
            operation.cancel();
        }
    }

    pub(crate) fn poll(&mut self) -> Option<InspectionResult> {
        let operation = self.active.as_mut()?;
        let outcome = operation.poll();
        let response = result(&operation.binding, outcome);
        if !operation.pending() {
            self.active = None;
        }
        Some(response)
    }
}

fn result(
    binding: &BoundRequest,
    outcome: Result<ValidatedObservation, Failure>,
) -> InspectionResult {
    InspectionResult {
        binding: binding.clone(),
        outcome: match outcome {
            Ok(observation) => Outcome::Observed(observation),
            Err(error) => Outcome::Failed(error),
        },
    }
}

pub(crate) struct Inspection<B: ReadOnlyBackend> {
    pub backend: B,
    inventory: TrustedInventory,
    binding: BoundRequest,
    started: SampleTime,
    deadline: Duration,
    descriptors: Option<Arc<DescriptorSet>>,
    queried: bool,
    cancelled: bool,
}

impl<B: ReadOnlyBackend> Inspection<B> {
    pub(crate) fn begin(
        policy: &CandidateInventory,
        binding: &BoundRequest,
        budget: ObservationBudget,
        mut backend: B,
    ) -> Result<Self, Failure> {
        if binding.0.inventory_generation != policy.generation() {
            return Err(Failure::Inconsistent(Inconsistent::InventoryBinding));
        }
        policy.role(&binding.0.role_id)?;
        let started = backend.clock();
        let deadline = started
            .monotonic
            .checked_add(budget.0)
            .ok_or(Failure::Unavailable(Unavailable::ArithmeticOverflow))?;
        let attestation = backend.attest(policy)?;
        validate_attestation(policy, &attestation)?;
        let inventory = TrustedInventory {
            policy: policy.clone(),
            attestation,
        };
        let mut operation = Self {
            backend,
            inventory,
            binding: binding.clone(),
            started,
            deadline,
            descriptors: None,
            queried: false,
            cancelled: false,
        };
        operation.check_deadline()?;
        operation.pin()?;
        operation.check_deadline()?;
        Ok(operation)
    }

    pub(crate) fn pending(&self) -> bool {
        self.descriptors.is_some()
    }

    #[cfg(test)]
    pub(crate) fn descriptor_count(&self) -> usize {
        self.descriptors.as_ref().map_or(0, |set| set.nodes.len())
    }

    pub(crate) fn cancel(&mut self) {
        self.cancelled = true;
    }

    fn check_deadline(&self) -> Result<(), Failure> {
        let now = self.backend.clock().monotonic;
        if now < self.started.monotonic {
            return Err(Failure::Unavailable(Unavailable::MalformedEvidence));
        }
        if now >= self.deadline {
            return Err(Failure::Unavailable(Unavailable::DeadlineExceeded));
        }
        if self.cancelled {
            return Err(Failure::Unavailable(Unavailable::Cancelled));
        }
        Ok(())
    }

    fn pin(&mut self) -> Result<(), Failure> {
        let role = self.inventory.policy.role(&self.binding.0.role_id)?;
        let root = self.backend.trusted_root(&self.inventory)?;
        let root_evidence = self.backend.node(root.as_fd())?;
        validate_node(&root_evidence, &self.inventory, None, None)?;
        let mut nodes = vec![PinnedDescriptor {
            descriptor: root,
            initial: root_evidence,
            edge: None,
        }];
        let relative = role
            .role
            .path
            .strip_prefix(&(self.inventory.policy.root.path.clone() + "/"))
            .ok_or(Failure::Inconsistent(Inconsistent::InventoryBinding))?;
        for component in relative.split('/') {
            self.check_deadline()?;
            let parent = nodes.len() - 1;
            let descriptor =
                self.backend
                    .open_component(nodes[parent].fd(), component, RESOLUTION)?;
            let evidence = self.backend.node(descriptor.as_fd())?;
            validate_node(&evidence, &self.inventory, Some(role), None)?;
            nodes.push(PinnedDescriptor {
                descriptor,
                initial: evidence,
                edge: Some((parent, component.to_owned())),
            });
        }
        let role_index = nodes.len() - 1;
        validate_project(&nodes[role_index].initial, role, true)?;
        let mut file_indices = Vec::with_capacity(role.existing_files.len());
        for file in &role.existing_files {
            self.check_deadline()?;
            let descriptor =
                self.backend
                    .open_component(nodes[role_index].fd(), &file.name, RESOLUTION)?;
            let evidence = self.backend.node(descriptor.as_fd())?;
            validate_node(&evidence, &self.inventory, Some(role), Some(file.mode))?;
            validate_project(&evidence, role, false)?;
            file_indices.push(nodes.len());
            nodes.push(PinnedDescriptor {
                descriptor,
                initial: evidence,
                edge: Some((role_index, file.name.clone())),
            });
        }
        if nodes.len() > MAX_DESCRIPTORS {
            return Err(Failure::Unavailable(Unavailable::ResourceLimit));
        }
        self.descriptors = Some(Arc::new(DescriptorSet {
            nodes,
            role_index,
            file_indices,
        }));
        self.revalidate()
    }

    fn revalidate(&mut self) -> Result<(), Failure> {
        let set = self
            .descriptors
            .as_ref()
            .ok_or(Failure::Unavailable(Unavailable::DescriptorUnavailable))?;
        let role = self.inventory.policy.role(&self.binding.0.role_id)?;
        self.backend
            .root_still_attested(&self.inventory, set.nodes[0].fd())?;
        let mut inodes = std::collections::HashSet::new();
        for (index, node) in set.nodes.iter().enumerate() {
            self.check_deadline()?;
            let current = self.backend.node(node.fd())?;
            let is_file = set.file_indices.contains(&index);
            validate_node(
                &current,
                &self.inventory,
                (index != 0).then_some(role),
                if is_file {
                    Some(node.initial.mode)
                } else {
                    None
                },
            )?;
            if current != node.initial {
                return Err(Failure::Inconsistent(Inconsistent::NodeIdentity));
            }
            if !inodes.insert(current.inode) {
                return Err(Failure::Inconsistent(Inconsistent::AliasedDomain));
            }
            if index == set.role_index || is_file {
                validate_project(&current, role, !is_file)?;
            }
            if let Some((parent, name)) = &node.edge {
                self.backend
                    .same_entry(set.nodes[*parent].fd(), name, node.fd(), RESOLUTION)?;
            }
            self.check_deadline()?;
        }
        Ok(())
    }

    pub(crate) fn poll(&mut self) -> Result<ValidatedObservation, Failure> {
        if self.descriptors.is_none() {
            return Err(Failure::Unavailable(Unavailable::DescriptorUnavailable));
        }
        // A started query must be polled to confirmed completion even after cancellation/expiry.
        if !self.queried
            && let Err(error) = self.check_deadline().and_then(|()| self.revalidate())
        {
            self.descriptors = None;
            return Err(error);
        }
        let query = if self.queried {
            self.backend.poll_quota()
        } else {
            let set = self.descriptors.as_ref().expect("owned descriptor set");
            let project = ProjectId(
                self.inventory
                    .policy
                    .role(&self.binding.0.role_id)?
                    .role
                    .project_id as u32,
            );
            self.queried = true;
            self.backend
                .read_project_quota(set.nodes[0].fd(), project, Arc::clone(set))
        };
        match query {
            QueryProgress::Pending(reason) => {
                self.check_deadline()?;
                Err(Failure::Unavailable(reason))
            }
            QueryProgress::Complete(completed) => {
                let outcome = (|| {
                    self.check_deadline()?;
                    let evidence = completed?;
                    self.revalidate()?;
                    let observation = validate_quota(
                        &self.inventory,
                        &self.binding,
                        self.started,
                        self.backend.clock(),
                        &evidence,
                    )?;
                    self.check_deadline()?;
                    Ok(observation)
                })();
                self.descriptors = None;
                outcome
            }
        }
    }
}

use std::os::fd::AsFd;

fn validate_attestation(
    policy: &CandidateInventory,
    attestation: &InventoryAttestation,
) -> Result<(), Failure> {
    if !hex(&attestation.approval_identity, 64)
        || attestation.policy_digest != policy.policy_digest()
        || attestation.generation != policy.generation()
    {
        return Err(Failure::Inconsistent(Inconsistent::InventoryBinding));
    }
    match attestation.consumer_namespace {
        None => Err(Failure::Unavailable(Unavailable::NamespaceUnavailable)),
        Some(namespace) if namespace != policy.root.filesystem.namespace => {
            Err(Failure::Inconsistent(Inconsistent::NamespaceMismatch))
        }
        Some(_) => Ok(()),
    }
}

fn validate_node(
    node: &NodeEvidence,
    inventory: &TrustedInventory,
    role: Option<&RoleDefinition>,
    file_mode: Option<u32>,
) -> Result<(), Failure> {
    let filesystem = node
        .filesystem
        .as_ref()
        .ok_or(Failure::Unavailable(Unavailable::MissingEvidence))?;
    if filesystem.namespace.inode == 0 {
        return Err(Failure::Unavailable(Unavailable::NamespaceUnavailable));
    }
    if filesystem.namespace != inventory.policy.root.filesystem.namespace {
        return Err(Failure::Inconsistent(Inconsistent::NamespaceMismatch));
    }
    if filesystem != &inventory.policy.root.filesystem {
        return Err(Failure::Inconsistent(Inconsistent::FilesystemIdentity));
    }
    if node.inode == 0 || node.links == 0 {
        return Err(Failure::Unavailable(Unavailable::MalformedEvidence));
    }
    let (uid, gid, mode) = match role {
        Some(role) => (
            role.role.owner_uid,
            role.role.owner_gid,
            file_mode.unwrap_or(role.mode),
        ),
        None => (
            u64::from(inventory.policy.root.owner_uid),
            u64::from(inventory.policy.root.owner_gid),
            inventory.policy.root.mode,
        ),
    };
    if u64::from(node.owner_uid) != uid || u64::from(node.owner_gid) != gid || node.mode != mode {
        return Err(Failure::Inconsistent(Inconsistent::Ownership));
    }
    if node.node_type
        != if file_mode.is_some() {
            NodeType::RegularFile
        } else {
            NodeType::Directory
        }
    {
        return Err(Failure::Inconsistent(Inconsistent::NodeIdentity));
    }
    if file_mode.is_some() && node.links != 1 {
        return Err(Failure::Inconsistent(Inconsistent::HardLinks));
    }
    Ok(())
}

fn validate_project(
    node: &NodeEvidence,
    role: &RoleDefinition,
    directory: bool,
) -> Result<(), Failure> {
    let attributes = node
        .project
        .ok_or(Failure::Unavailable(Unavailable::MissingEvidence))?;
    if u64::from(attributes.project_id.0) != role.role.project_id {
        return Err(Failure::Inconsistent(Inconsistent::ProjectIdentity));
    }
    if directory && !attributes.inheritance {
        return Err(Failure::Inconsistent(Inconsistent::Inheritance));
    }
    Ok(())
}

fn validate_quota(
    inventory: &TrustedInventory,
    binding: &BoundRequest,
    started: SampleTime,
    now: SampleTime,
    evidence: &QuotaEvidence,
) -> Result<ValidatedObservation, Failure> {
    let role = inventory.policy.role(&binding.0.role_id)?;
    let reading = &evidence.after;
    if evidence.sampled_at.monotonic < started.monotonic
        || evidence.sampled_at.monotonic > now.monotonic
        || evidence.sampled_at.unix_seconds > now.unix_seconds.saturating_add(2)
        || now
            .unix_seconds
            .saturating_sub(evidence.sampled_at.unix_seconds)
            > MAX_AGE_SECONDS
    {
        return Err(Failure::Unavailable(Unavailable::StaleEvidence));
    }
    if evidence.before != evidence.after {
        return Err(Failure::Inconsistent(Inconsistent::ChangedEvidence));
    }
    if reading.filesystem.namespace.inode == 0 {
        return Err(Failure::Unavailable(Unavailable::NamespaceUnavailable));
    }
    if reading.filesystem.namespace != inventory.policy.root.filesystem.namespace {
        return Err(Failure::Inconsistent(Inconsistent::NamespaceMismatch));
    }
    if reading.filesystem != inventory.policy.root.filesystem {
        return Err(Failure::Inconsistent(Inconsistent::FilesystemIdentity));
    }
    if reading.state == QuotaState::Unknown {
        return Err(Failure::Unavailable(Unavailable::MissingEvidence));
    }
    if reading.state != QuotaState::Enforced {
        return Err(Failure::Inconsistent(Inconsistent::QuotaNotEnforced));
    }
    if u64::from(reading.project_id.0) != role.role.project_id {
        return Err(Failure::Inconsistent(Inconsistent::ProjectIdentity));
    }
    if reading.limits
        != (HardLimits {
            bytes: role.role.byte_limit,
            inodes: role.role.inode_limit,
        })
    {
        return Err(Failure::Inconsistent(Inconsistent::HardLimits));
    }
    if reading.usage.bytes > reading.limits.bytes || reading.usage.inodes > reading.limits.inodes {
        return Err(Failure::Inconsistent(Inconsistent::Usage));
    }
    let capacity = reading.capacity;
    if capacity.fragment_bytes == 0 || capacity.blocks == 0 || capacity.inodes == 0 {
        return Err(Failure::Unavailable(Unavailable::MalformedEvidence));
    }
    if capacity.available_blocks > capacity.blocks || capacity.available_inodes > capacity.inodes {
        return Err(Failure::Inconsistent(Inconsistent::Usage));
    }
    let filesystem_bytes = capacity
        .blocks
        .checked_mul(capacity.fragment_bytes)
        .ok_or(Failure::Unavailable(Unavailable::ArithmeticOverflow))?;
    let filesystem_bytes_available = capacity
        .available_blocks
        .checked_mul(capacity.fragment_bytes)
        .ok_or(Failure::Unavailable(Unavailable::ArithmeticOverflow))?;
    if reading.usage.bytes > filesystem_bytes || reading.usage.inodes > capacity.inodes {
        return Err(Failure::Inconsistent(Inconsistent::Usage));
    }
    Ok(ValidatedObservation {
        filesystem_uuid: reading.filesystem.uuid.clone(),
        mount_target: role.role.mount_path.clone(),
        mount_id: reading.filesystem.mount_id,
        project_id: reading.project_id,
        limits: reading.limits,
        usage: reading.usage,
        filesystem_bytes,
        filesystem_bytes_available,
        filesystem_inodes: capacity.inodes,
        filesystem_inodes_available: capacity.available_inodes,
        owner_uid: role.role.owner_uid as u32,
        owner_gid: role.role.owner_gid as u32,
        observed_at: evidence.sampled_at.unix_seconds,
    })
}
