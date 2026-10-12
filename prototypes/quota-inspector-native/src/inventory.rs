use std::collections::HashSet;

use quota_inspector_protocol::{StorageRole, inventory_generation};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::{Failure, Inconsistent, Unavailable};

pub const MAX_INVENTORY_BYTES: usize = 16_384;
pub const MAX_ROLES: usize = 32;
pub const MAX_EXISTING_FILES: usize = 4;
pub const MAX_COMPONENTS: usize = 8;
pub const HARD_ENTITLEMENTS: u64 = 7_680 * 1024 * 1024;
pub const HARD_INODE_ENTITLEMENTS: u64 = 221_184;
pub const CONTINUOUS_RESERVE: u64 = 8_192 * 1024 * 1024;
pub const OPERATIONAL_ALLOWANCE: u64 = 256 * 1024 * 1024;
pub const INITIAL_AVAILABLE_BYTES: u64 = 16_128 * 1024 * 1024;
pub const INITIAL_AVAILABLE_INODES: u64 = 241_664;
pub const MAX_PLANNED_STORAGE: u64 = 128 * 1024 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Purpose {
    TaskSlot,
    PodmanGraph,
    PodmanRuntime,
    ImageScratch,
    BrokerState,
    BrokerLogs,
    Temporary,
}

impl Purpose {
    pub(crate) fn kind(self) -> &'static str {
        match self {
            Self::TaskSlot => "task_slot",
            Self::PodmanGraph => "podman_graph",
            Self::PodmanRuntime => "podman_runtime",
            Self::ImageScratch => "image_scratch",
            Self::BrokerState => "broker_state",
            Self::BrokerLogs => "broker_logs",
            Self::Temporary => "temporary",
        }
    }

    fn ceilings(self) -> (u64, u64) {
        match self {
            Self::TaskSlot => (256 * 1024 * 1024, 4096),
            Self::PodmanGraph => (4 * 1024 * 1024 * 1024, 131_072),
            Self::BrokerLogs => (512 * 1024 * 1024, 8192),
            _ => (512 * 1024 * 1024, 16_384),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceId {
    pub major: u32,
    pub minor: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NamespaceId {
    pub device: u64,
    pub inode: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FilesystemIdentity {
    pub uuid: String,
    pub device: DeviceId,
    pub mount_id: u64,
    pub mount_root: String,
    pub filesystem_type: String,
    pub namespace: NamespaceId,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RootDefinition {
    pub path: String,
    pub owner_uid: u32,
    pub owner_gid: u32,
    pub mode: u32,
    pub filesystem: FilesystemIdentity,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PersistentPurpose {
    SqliteDb,
    SqliteWal,
    SqliteShm,
    Log,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ExistingFile {
    pub name: String,
    pub purpose: PersistentPurpose,
    pub mode: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RoleDefinition {
    pub role: StorageRole,
    pub purpose: Purpose,
    pub mode: u32,
    pub existing_files: Vec<ExistingFile>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Document {
    root: RootDefinition,
    roles: Vec<RoleDefinition>,
    inventory_generation: String,
}

/// Parsed policy is not authenticated inventory authority.
#[derive(Debug, Clone)]
pub struct CandidateInventory {
    pub(crate) root: RootDefinition,
    pub(crate) roles: Vec<RoleDefinition>,
    generation: String,
    digest: String,
}

impl CandidateInventory {
    pub fn parse(bytes: &[u8]) -> Result<Self, Failure> {
        if bytes.is_empty() || bytes.len() > MAX_INVENTORY_BYTES {
            return Err(Failure::Unavailable(Unavailable::InventoryInvalid));
        }
        let mut depth = 0usize;
        let mut quoted = false;
        let mut escaped = false;
        for byte in bytes {
            if quoted {
                if escaped {
                    escaped = false;
                } else if *byte == b'\\' {
                    escaped = true;
                } else if *byte == b'"' {
                    quoted = false;
                }
            } else {
                match byte {
                    b'"' => quoted = true,
                    b'{' | b'[' => {
                        depth += 1;
                        if depth > 16 {
                            return Err(Failure::Unavailable(Unavailable::InventoryInvalid));
                        }
                    }
                    b'}' | b']' => depth = depth.saturating_sub(1),
                    _ => {}
                }
            }
        }
        let document: Document = serde_json::from_slice(bytes)
            .map_err(|_| Failure::Unavailable(Unavailable::InventoryInvalid))?;
        let candidate = Self {
            root: document.root,
            roles: document.roles,
            generation: document.inventory_generation,
            digest: format!("{:x}", Sha256::digest(bytes)),
        };
        candidate.validate()?;
        Ok(candidate)
    }

    pub fn generation(&self) -> &str {
        &self.generation
    }

    /// Binds the complete native policy bytes, not just the ten v1 role fields.
    pub fn policy_digest(&self) -> &str {
        &self.digest
    }

    pub fn role_count(&self) -> usize {
        self.roles.len()
    }

    pub(crate) fn role(&self, id: &str) -> Result<&RoleDefinition, Failure> {
        self.roles
            .iter()
            .find(|role| role.role.role_id == id)
            .ok_or(Failure::Unavailable(Unavailable::RequestInvalid))
    }

    fn validate(&self) -> Result<(), Failure> {
        let invalid = Failure::Unavailable(Unavailable::InventoryInvalid);
        if !canonical_path(&self.root.path)
            || self.root.path == "/"
            || !safe_mode(self.root.mode)
            || self.roles.is_empty()
            || self.roles.len() > MAX_ROLES
            || !canonical_uuid(&self.root.filesystem.uuid)
            || self.root.filesystem.mount_id == 0
            || self.root.filesystem.namespace.inode == 0
            || self.root.filesystem.filesystem_type != "ext4"
            || self.root.filesystem.mount_root != "/"
            || self.root.filesystem.device == (DeviceId { major: 0, minor: 0 })
        {
            return Err(invalid);
        }
        let mut ids = HashSet::new();
        let mut projects = HashSet::new();
        let mut purposes = HashSet::new();
        let mut entitlement = 0u64;
        let mut inode_entitlement = 0u64;
        for definition in &self.roles {
            let role = &definition.role;
            let (bytes, inodes) = definition.purpose.ceilings();
            let relative = role.path.strip_prefix(&(self.root.path.clone() + "/"));
            if !role_id(&role.role_id)
                || !ids.insert(&role.role_id)
                || !projects.insert(role.project_id)
                || (!purposes.insert(definition.purpose) && definition.purpose != Purpose::TaskSlot)
                || role.kind != definition.purpose.kind()
                || !canonical_path(&role.path)
                || relative.is_none_or(|value| value.split('/').count() > MAX_COMPONENTS)
                || role.mount_path != self.root.path
                || role.filesystem_uuid != self.root.filesystem.uuid
                || role.project_id == 0
                || role.project_id > u32::MAX as u64
                || !(1024..=bytes).contains(&role.byte_limit)
                || !role.byte_limit.is_multiple_of(1024)
                || !(16..=inodes).contains(&role.inode_limit)
                || role.owner_uid > u32::MAX as u64
                || role.owner_gid > u32::MAX as u64
                || !safe_mode(definition.mode)
                || definition.existing_files.len() > MAX_EXISTING_FILES
            {
                return Err(invalid);
            }
            entitlement = entitlement.checked_add(role.byte_limit).ok_or(invalid)?;
            inode_entitlement = inode_entitlement
                .checked_add(role.inode_limit)
                .ok_or(invalid)?;
            let mut names = HashSet::new();
            let mut file_purposes = HashSet::new();
            for file in &definition.existing_files {
                if !component(&file.name)
                    || !names.insert(&file.name)
                    || !file_purposes.insert(file.purpose)
                    || file.mode != 0o600
                    || match definition.purpose {
                        Purpose::BrokerState => file.purpose == PersistentPurpose::Log,
                        Purpose::BrokerLogs => file.purpose != PersistentPurpose::Log,
                        _ => true,
                    }
                {
                    return Err(invalid);
                }
            }
            let required = match definition.purpose {
                Purpose::BrokerState => 3,
                Purpose::BrokerLogs => 1,
                _ => 0,
            };
            if definition.existing_files.len() != required {
                return Err(invalid);
            }
        }
        if purposes.len() != 7
            || entitlement > HARD_ENTITLEMENTS
            || inode_entitlement > HARD_INODE_ENTITLEMENTS
        {
            return Err(invalid);
        }
        for (index, left) in self.roles.iter().enumerate() {
            for right in &self.roles[index + 1..] {
                if beneath_or_equal(&left.role.path, &right.role.path)
                    || beneath_or_equal(&right.role.path, &left.role.path)
                {
                    return Err(Failure::Inconsistent(Inconsistent::AliasedDomain));
                }
                let left_components: Vec<_> = left.role.path.split('/').collect();
                let right_components: Vec<_> = right.role.path.split('/').collect();
                let shared = left_components
                    .iter()
                    .zip(right_components)
                    .take_while(|(a, b)| **a == *b)
                    .count();
                if shared > self.root.path.split('/').count()
                    && (left.mode != right.mode
                        || left.role.owner_uid != right.role.owner_uid
                        || left.role.owner_gid != right.role.owner_gid)
                {
                    return Err(invalid);
                }
            }
        }
        let roles: Vec<_> = self.roles.iter().map(|role| role.role.clone()).collect();
        if self.generation != inventory_generation(&roles) {
            return Err(invalid);
        }
        Ok(())
    }
}

pub(crate) fn component(value: &str) -> bool {
    !value.is_empty()
        && value != "."
        && value != ".."
        && value.len() <= 255
        && !value.contains(['/', '\0'])
}

pub(crate) fn canonical_path(value: &str) -> bool {
    value.starts_with('/')
        && value.len() <= 1024
        && (value == "/" || value[1..].split('/').all(component))
}

pub(crate) fn canonical_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| {
            if [8, 13, 18, 23].contains(&index) {
                byte == b'-'
            } else {
                byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
            }
        })
}

pub(crate) fn role_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 64
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_-".contains(byte))
}

pub(crate) fn hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn beneath_or_equal(path: &str, ancestor: &str) -> bool {
    path == ancestor
        || path
            .strip_prefix(ancestor)
            .is_some_and(|suffix| suffix.starts_with('/'))
}

fn safe_mode(mode: u32) -> bool {
    matches!(mode, 0o700 | 0o750)
}
