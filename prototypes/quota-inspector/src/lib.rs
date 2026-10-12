#![forbid(unsafe_code)]

use std::collections::{HashMap, VecDeque};
use std::io::{self, Read, Write};
use std::os::fd::AsFd;
use std::os::unix::fs::{DirBuilderExt, FileTypeExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::de::{MapAccess, Visitor};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fmt;

pub const MAX_FRAME_BYTES: usize = 16 * 1024;
pub const MAX_ROLES: usize = 32;
pub const REPLAY_CAPACITY: usize = 1024;
pub const REPLAY_TTL: Duration = Duration::from_secs(30);
pub const CONNECTION_TIMEOUT: Duration = Duration::from_millis(500);
pub const MAX_LIFETIME: Duration = Duration::from_secs(30);
const MAX_JSON_DEPTH: usize = 16;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StorageRole {
    pub role_id: String,
    pub kind: String,
    pub path: String,
    pub mount_path: String,
    pub filesystem_uuid: String,
    pub project_id: u64,
    pub byte_limit: u64,
    pub inode_limit: u64,
    pub owner_uid: u64,
    pub owner_gid: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResponseValidationError {
    Invalid,
    Stale,
    Inconsistent,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Observation {
    pub filesystem_uuid: String,
    pub mount_target: String,
    pub mount_id: u64,
    pub filesystem_type: String,
    pub quota_mode: String,
    pub quota_configuration_enabled: bool,
    pub project_id: u64,
    pub inheritance_enabled: bool,
    pub byte_limit: u64,
    pub inode_limit: u64,
    pub bytes_used: u64,
    pub inodes_used: u64,
    pub filesystem_bytes: u64,
    pub filesystem_bytes_available: u64,
    pub filesystem_inodes: u64,
    pub filesystem_inodes_available: u64,
    pub owner_uid: u64,
    pub owner_gid: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct InventoryFile {
    label: String,
    roles: Vec<StorageRole>,
    #[serde(deserialize_with = "deserialize_unique_observations")]
    observations: HashMap<String, Observation>,
    expected_generation: String,
}

fn deserialize_unique_observations<'de, D>(
    deserializer: D,
) -> Result<HashMap<String, Observation>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    struct UniqueObservations;

    impl<'de> Visitor<'de> for UniqueObservations {
        type Value = HashMap<String, Observation>;

        fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            formatter.write_str("an object with unique role observation keys")
        }

        fn visit_map<A>(self, mut access: A) -> Result<Self::Value, A::Error>
        where
            A: MapAccess<'de>,
        {
            let mut observations = HashMap::new();
            while let Some((key, observation)) = access.next_entry::<String, Observation>()? {
                if observations.insert(key, observation).is_some() {
                    return Err(serde::de::Error::custom("duplicate observation role key"));
                }
            }
            Ok(observations)
        }
    }

    deserializer.deserialize_map(UniqueObservations)
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub schema_version: u8,
    pub message_type: String,
    pub role_id: String,
    pub challenge: String,
    pub inventory_generation: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Response {
    pub schema_version: u8,
    pub message_type: String,
    pub role_id: String,
    pub challenge: String,
    pub status: String,
    pub helper_boot_epoch: String,
    pub inventory_generation: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub observed_at: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub filesystem_uuid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mount_target: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mount_id: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub filesystem_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quota_mode: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quota_configuration_enabled: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub project_id: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inheritance_enabled: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub byte_limit: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inode_limit: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bytes_used: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inodes_used: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub filesystem_bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub filesystem_bytes_available: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub filesystem_inodes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub filesystem_inodes_available: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub owner_uid: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub owner_gid: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_code: Option<String>,
}

impl Response {
    fn has_observation_fields(&self) -> bool {
        self.observed_at.is_some()
            || self.filesystem_uuid.is_some()
            || self.mount_target.is_some()
            || self.mount_id.is_some()
            || self.filesystem_type.is_some()
            || self.quota_mode.is_some()
            || self.quota_configuration_enabled.is_some()
            || self.project_id.is_some()
            || self.inheritance_enabled.is_some()
            || self.byte_limit.is_some()
            || self.inode_limit.is_some()
            || self.bytes_used.is_some()
            || self.inodes_used.is_some()
            || self.filesystem_bytes.is_some()
            || self.filesystem_bytes_available.is_some()
            || self.filesystem_inodes.is_some()
            || self.filesystem_inodes_available.is_some()
            || self.owner_uid.is_some()
            || self.owner_gid.is_some()
    }

    fn has_all_observation_fields(&self) -> bool {
        self.observed_at.is_some()
            && self.filesystem_uuid.is_some()
            && self.mount_target.is_some()
            && self.mount_id.is_some()
            && self.filesystem_type.is_some()
            && self.quota_mode.is_some()
            && self.quota_configuration_enabled.is_some()
            && self.project_id.is_some()
            && self.inheritance_enabled.is_some()
            && self.byte_limit.is_some()
            && self.inode_limit.is_some()
            && self.bytes_used.is_some()
            && self.inodes_used.is_some()
            && self.filesystem_bytes.is_some()
            && self.filesystem_bytes_available.is_some()
            && self.filesystem_inodes.is_some()
            && self.filesystem_inodes_available.is_some()
            && self.owner_uid.is_some()
            && self.owner_gid.is_some()
    }
}

#[derive(Debug, Clone)]
pub struct Inventory {
    pub label: String,
    pub roles: Vec<StorageRole>,
    pub observations: HashMap<String, Observation>,
    pub generation: String,
}

struct HarnessSettings<'a> {
    inventory: &'a Inventory,
    allowed_uid: u32,
    allowed_gid: u32,
    backend: Backend,
}

struct OwnedRuntimeDirectory {
    path: PathBuf,
    device: u64,
    inode: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Backend {
    Fixture,
    NativeUnavailable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerCredentials {
    pub pid: i32,
    pub uid: u32,
    pub gid: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReplayDecision {
    New,
    Replayed,
    Full,
}

#[derive(Debug, Default)]
pub struct ReplayCache {
    entries: HashMap<String, Instant>,
}

impl ReplayCache {
    pub fn check_and_insert(&mut self, challenge: &str, now: Instant) -> ReplayDecision {
        self.entries.retain(|_, inserted| {
            now.checked_duration_since(*inserted)
                .is_none_or(|age| age < REPLAY_TTL)
        });
        if self.entries.contains_key(challenge) {
            return ReplayDecision::Replayed;
        }
        if self.entries.len() >= REPLAY_CAPACITY {
            return ReplayDecision::Full;
        }
        self.entries.insert(challenge.to_owned(), now);
        ReplayDecision::New
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

impl Inventory {
    pub fn load(path: &Path) -> Result<Self, String> {
        let path_metadata = std::fs::symlink_metadata(path)
            .map_err(|error| format!("cannot inspect inventory file: {error}"))?;
        if !path_metadata.file_type().is_file() || path_metadata.len() > MAX_FRAME_BYTES as u64 {
            return Err("inventory must be a regular file no larger than 16 KiB".to_owned());
        }
        let descriptor = rustix::fs::open(
            path,
            rustix::fs::OFlags::RDONLY
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::NONBLOCK
                | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        )
        .map_err(|error| format!("cannot open inventory without following links: {error}"))?;
        let stat = rustix::fs::fstat(&descriptor)
            .map_err(|error| format!("cannot verify inventory file: {error}"))?;
        if !rustix::fs::FileType::from_raw_mode(stat.st_mode).is_file()
            || stat.st_dev != path_metadata.dev()
            || stat.st_ino != path_metadata.ino()
            || stat.st_size < 0
            || stat.st_size as u64 > MAX_FRAME_BYTES as u64
        {
            return Err("inventory must be a stable regular file no larger than 16 KiB".to_owned());
        }
        let file = std::fs::File::from(descriptor);
        let mut bytes = Vec::new();
        file.take(MAX_FRAME_BYTES as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|error| format!("cannot read inventory: {error}"))?;
        if bytes.len() > MAX_FRAME_BYTES {
            return Err("inventory exceeds 16 KiB".to_owned());
        }
        let text = std::str::from_utf8(&bytes).map_err(|_| "inventory is not UTF-8")?;
        check_json_depth(text, MAX_JSON_DEPTH)?;
        let input: InventoryFile =
            serde_json::from_str(text).map_err(|error| format!("invalid inventory: {error}"))?;
        Self::from_file(input)
    }

    pub fn from_json(text: &str) -> Result<Self, String> {
        check_json_depth(text, MAX_JSON_DEPTH)?;
        let input: InventoryFile =
            serde_json::from_str(text).map_err(|error| format!("invalid inventory: {error}"))?;
        Self::from_file(input)
    }

    fn from_file(file: InventoryFile) -> Result<Self, String> {
        let expected_generation = file.expected_generation;
        let inventory = Self {
            label: file.label,
            generation: inventory_generation(&file.roles),
            roles: file.roles,
            observations: file.observations,
        };
        inventory.validate()?;
        if inventory.generation != expected_generation {
            return Err("inventory generation does not match its roles".to_owned());
        }
        Ok(inventory)
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.label != "synthetic_offline_only"
            || self.roles.is_empty()
            || self.roles.len() > MAX_ROLES
        {
            return Err("inventory label or role count is invalid".to_owned());
        }
        let mut role_ids = std::collections::HashSet::new();
        for role in &self.roles {
            validate_role(role)?;
            if !role_ids.insert(role.role_id.as_str()) {
                return Err("duplicate role id".to_owned());
            }
        }
        if self.observations.len() != self.roles.len() {
            return Err("inventory observations do not match role set".to_owned());
        }
        for role in &self.roles {
            let observation = self
                .observations
                .get(&role.role_id)
                .ok_or_else(|| "inventory observation is missing".to_owned())?;
            validate_observation(role, observation)?;
        }
        if self
            .observations
            .keys()
            .any(|key| !role_ids.contains(key.as_str()))
        {
            return Err("inventory contains an unknown observation".to_owned());
        }
        if self.generation != inventory_generation(&self.roles) {
            return Err("inventory generation does not match its roles".to_owned());
        }
        Ok(())
    }

    pub fn response(
        &self,
        request: &Request,
        epoch: &str,
        observed_at: u64,
        backend: Backend,
    ) -> Result<Response, &'static str> {
        self.validate().map_err(|_| "invalid inventory")?;
        validate_request(request, self)?;
        if !valid_hex(epoch, 32) {
            return Err("invalid helper epoch");
        }
        let common = Response {
            schema_version: 1,
            message_type: "storage_inspection_result".to_owned(),
            role_id: request.role_id.clone(),
            challenge: request.challenge.clone(),
            status: String::new(),
            helper_boot_epoch: epoch.to_owned(),
            inventory_generation: self.generation.clone(),
            observed_at: None,
            filesystem_uuid: None,
            mount_target: None,
            mount_id: None,
            filesystem_type: None,
            quota_mode: None,
            quota_configuration_enabled: None,
            project_id: None,
            inheritance_enabled: None,
            byte_limit: None,
            inode_limit: None,
            bytes_used: None,
            inodes_used: None,
            filesystem_bytes: None,
            filesystem_bytes_available: None,
            filesystem_inodes: None,
            filesystem_inodes_available: None,
            owner_uid: None,
            owner_gid: None,
            error_code: None,
        };
        if backend == Backend::NativeUnavailable {
            return Ok(Response {
                status: "unavailable".to_owned(),
                error_code: Some("native_backend_unavailable".to_owned()),
                ..common
            });
        }
        let observation = self
            .observations
            .get(&request.role_id)
            .ok_or("inventory observation is missing")?;
        Ok(Response {
            status: "ok".to_owned(),
            observed_at: Some(observed_at),
            filesystem_uuid: Some(observation.filesystem_uuid.clone()),
            mount_target: Some(observation.mount_target.clone()),
            mount_id: Some(observation.mount_id),
            filesystem_type: Some(observation.filesystem_type.clone()),
            quota_mode: Some(observation.quota_mode.clone()),
            quota_configuration_enabled: Some(observation.quota_configuration_enabled),
            project_id: Some(observation.project_id),
            inheritance_enabled: Some(observation.inheritance_enabled),
            byte_limit: Some(observation.byte_limit),
            inode_limit: Some(observation.inode_limit),
            bytes_used: Some(observation.bytes_used),
            inodes_used: Some(observation.inodes_used),
            filesystem_bytes: Some(observation.filesystem_bytes),
            filesystem_bytes_available: Some(observation.filesystem_bytes_available),
            filesystem_inodes: Some(observation.filesystem_inodes),
            filesystem_inodes_available: Some(observation.filesystem_inodes_available),
            owner_uid: Some(observation.owner_uid),
            owner_gid: Some(observation.owner_gid),
            ..common
        })
    }
}

pub fn inventory_generation(roles: &[StorageRole]) -> String {
    let mut sorted: Vec<&StorageRole> = roles.iter().collect();
    sorted.sort_by(|left, right| left.role_id.cmp(&right.role_id));
    let mut canonical = String::from("[");
    for (index, role) in sorted.iter().enumerate() {
        if index != 0 {
            canonical.push(',');
        }
        canonical.push('{');
        let fields = [
            ("byte_limit", role.byte_limit.to_string()),
            ("filesystem_uuid", python_json_string(&role.filesystem_uuid)),
            ("inode_limit", role.inode_limit.to_string()),
            ("kind", python_json_string(&role.kind)),
            ("mount_path", python_json_string(&role.mount_path)),
            ("owner_gid", role.owner_gid.to_string()),
            ("owner_uid", role.owner_uid.to_string()),
            ("path", python_json_string(&role.path)),
            ("project_id", role.project_id.to_string()),
            ("role_id", python_json_string(&role.role_id)),
        ];
        for (field_index, (name, value)) in fields.iter().enumerate() {
            if field_index != 0 {
                canonical.push(',');
            }
            canonical.push_str(&python_json_string(name));
            canonical.push(':');
            canonical.push_str(value);
        }
        canonical.push('}');
    }
    canonical.push(']');
    let digest = Sha256::digest(canonical.as_bytes());
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn python_json_string(value: &str) -> String {
    let mut result = String::from("\"");
    for character in value.chars() {
        match character {
            '"' => result.push_str("\\\""),
            '\\' => result.push_str("\\\\"),
            '\u{08}' => result.push_str("\\b"),
            '\u{0c}' => result.push_str("\\f"),
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            c if c <= '\u{1f}' => result.push_str(&format!("\\u{:04x}", c as u32)),
            c if c.is_ascii() => result.push(c),
            c if (c as u32) <= 0xffff => result.push_str(&format!("\\u{:04x}", c as u32)),
            c => {
                let code = c as u32 - 0x10000;
                let high = 0xd800 + (code >> 10);
                let low = 0xdc00 + (code & 0x3ff);
                result.push_str(&format!("\\u{high:04x}\\u{low:04x}"));
            }
        }
    }
    result.push('"');
    result
}

pub fn validate_request(request: &Request, inventory: &Inventory) -> Result<(), &'static str> {
    if request.schema_version != 1
        || request.message_type != "inspect_storage_role"
        || !valid_role_id(&request.role_id)
        || !valid_hex(&request.challenge, 64)
        || request.inventory_generation != inventory.generation
        || !inventory
            .roles
            .iter()
            .any(|role| role.role_id == request.role_id)
    {
        return Err("invalid request");
    }
    Ok(())
}

pub fn parse_request(payload: &[u8], inventory: &Inventory) -> Result<Request, String> {
    if payload.is_empty() || payload.len() > MAX_FRAME_BYTES {
        return Err("request frame length is invalid".to_owned());
    }
    let text = std::str::from_utf8(payload).map_err(|_| "request is not UTF-8")?;
    check_json_depth(text, MAX_JSON_DEPTH)?;
    let request: Request =
        serde_json::from_str(text).map_err(|error| format!("invalid request JSON: {error}"))?;
    validate_request(&request, inventory).map_err(str::to_owned)?;
    Ok(request)
}

pub fn parse_response(
    payload: &[u8],
    request: &Request,
    inventory: &Inventory,
    now: u64,
    max_age_seconds: u64,
) -> Result<Response, ResponseValidationError> {
    if payload.is_empty() || payload.len() > MAX_FRAME_BYTES {
        return Err(ResponseValidationError::Invalid);
    }
    let text = std::str::from_utf8(payload).map_err(|_| ResponseValidationError::Invalid)?;
    check_json_depth(text, MAX_JSON_DEPTH).map_err(|_| ResponseValidationError::Invalid)?;
    let response: Response =
        serde_json::from_str(text).map_err(|_| ResponseValidationError::Invalid)?;
    validate_response(&response, request, inventory, now, max_age_seconds)?;
    Ok(response)
}

pub fn validate_response(
    response: &Response,
    request: &Request,
    inventory: &Inventory,
    now: u64,
    max_age_seconds: u64,
) -> Result<(), ResponseValidationError> {
    let invalid = ResponseValidationError::Invalid;
    if response.schema_version != 1
        || response.message_type != "storage_inspection_result"
        || response.role_id != request.role_id
        || response.challenge != request.challenge
        || response.inventory_generation != inventory.generation
        || !valid_hex(&response.helper_boot_epoch, 32)
    {
        return Err(invalid);
    }
    match response.status.as_str() {
        "unavailable" | "inconsistent" => {
            if response
                .error_code
                .as_deref()
                .is_none_or(|code| !valid_role_id(code))
                || response.has_observation_fields()
            {
                return Err(invalid);
            }
            Ok(())
        }
        "ok" => {
            if response.error_code.is_some() || !response.has_all_observation_fields() {
                return Err(invalid);
            }
            let observed_at = response.observed_at.ok_or(invalid)?;
            if observed_at > now.saturating_add(2)
                || now.saturating_sub(observed_at) > max_age_seconds
            {
                return Err(ResponseValidationError::Stale);
            }
            let filesystem_uuid = response.filesystem_uuid.as_deref().ok_or(invalid)?;
            let mount_target = response.mount_target.as_deref().ok_or(invalid)?;
            if !valid_uuid(filesystem_uuid)
                || !canonical_absolute_path(mount_target)
                || response.filesystem_type.as_deref() != Some("ext4")
                || response.quota_mode.as_deref() != Some("project")
            {
                return Err(invalid);
            }
            let mount_id = response.mount_id.ok_or(invalid)?;
            let byte_limit = response.byte_limit.ok_or(invalid)?;
            let inode_limit = response.inode_limit.ok_or(invalid)?;
            let bytes_used = response.bytes_used.ok_or(invalid)?;
            let inodes_used = response.inodes_used.ok_or(invalid)?;
            let filesystem_bytes = response.filesystem_bytes.ok_or(invalid)?;
            let filesystem_bytes_available = response.filesystem_bytes_available.ok_or(invalid)?;
            let filesystem_inodes = response.filesystem_inodes.ok_or(invalid)?;
            let filesystem_inodes_available =
                response.filesystem_inodes_available.ok_or(invalid)?;
            if mount_id == 0 {
                return Err(invalid);
            }
            if byte_limit == 0
                || inode_limit == 0
                || bytes_used > byte_limit
                || inodes_used > inode_limit
                || filesystem_bytes_available > filesystem_bytes
                || filesystem_inodes_available > filesystem_inodes
            {
                return Err(ResponseValidationError::Inconsistent);
            }
            Ok(())
        }
        _ => Err(invalid),
    }
}

pub fn response_json(response: &Response) -> Result<Vec<u8>, serde_json::Error> {
    serde_json::to_vec(response)
}

pub fn decode_frame<R: Read>(reader: &mut R) -> io::Result<Vec<u8>> {
    let mut header = [0_u8; 4];
    reader.read_exact(&mut header)?;
    let length = u32::from_be_bytes(header) as usize;
    if !(1..=MAX_FRAME_BYTES).contains(&length) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid frame size",
        ));
    }
    let mut payload = vec![0; length];
    reader.read_exact(&mut payload)?;
    Ok(payload)
}

pub fn encode_frame(payload: &[u8]) -> io::Result<Vec<u8>> {
    if payload.is_empty() || payload.len() > MAX_FRAME_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid frame size",
        ));
    }
    let mut result = Vec::with_capacity(payload.len() + 4);
    result.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    result.extend_from_slice(payload);
    Ok(result)
}

pub fn check_peer(credentials: PeerCredentials, allowed_uid: u32, allowed_gid: u32) -> bool {
    credentials.pid > 0 && credentials.uid == allowed_uid && credentials.gid == allowed_gid
}

pub fn peer_credentials(stream: &UnixStream) -> io::Result<PeerCredentials> {
    let credentials = rustix::net::sockopt::socket_peercred(stream.as_fd())?;
    Ok(PeerCredentials {
        pid: credentials.pid.as_raw_nonzero().get(),
        uid: credentials.uid.as_raw(),
        gid: credentials.gid.as_raw(),
    })
}

pub fn random_hex(byte_count: usize) -> Result<String, String> {
    let mut bytes = vec![0; byte_count];
    getrandom::fill(&mut bytes).map_err(|error| format!("OS randomness unavailable: {error}"))?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

pub fn unix_time_seconds() -> Result<u64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .map_err(|error| format!("system clock precedes Unix epoch: {error}"))
}

pub fn run_harness(
    parent: &Path,
    inventory: &Inventory,
    allowed_uid: u32,
    allowed_gid: u32,
    lifetime: Duration,
    backend: Backend,
) -> Result<(), String> {
    inventory.validate()?;
    if lifetime.is_zero() || lifetime > MAX_LIFETIME {
        return Err("lifetime must be between 1 ms and 30000 ms".to_owned());
    }
    if unsafe_root() {
        return Err("fixture harness refuses to run as root".to_owned());
    }
    let effective_uid = rustix::process::geteuid().as_raw();
    verify_parent(parent, effective_uid)?;
    let child_dir = owned_child_directory(parent)?;
    let socket_path = child_dir.path.join("inspector.sock");
    let mut socket_identity = None;
    let settings = HarnessSettings {
        inventory,
        allowed_uid,
        allowed_gid,
        backend,
    };
    let result = run_in_directory(&socket_path, &settings, lifetime, &mut socket_identity);
    cleanup_owned(
        &child_dir.path,
        (child_dir.device, child_dir.inode),
        &socket_path,
        socket_identity,
    );
    result
}

fn unsafe_root() -> bool {
    rustix::process::geteuid().as_raw() == 0
}

fn verify_parent(parent: &Path, uid_hint: u32) -> Result<(), String> {
    if parent.to_str().is_none() {
        return Err("parent path must be UTF-8 for the startup protocol".to_owned());
    }
    let canonical = parent
        .canonicalize()
        .map_err(|error| format!("invalid parent: {error}"))?;
    if canonical != parent {
        return Err("parent directory must be canonical".to_owned());
    }
    let metadata =
        std::fs::symlink_metadata(parent).map_err(|error| format!("invalid parent: {error}"))?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != uid_hint
        || metadata.permissions().mode() & 0o777 != 0o700
    {
        return Err("parent directory must be owned by this user with mode 0700".to_owned());
    }
    let mut current = PathBuf::from("/");
    for part in parent.components().skip(1) {
        current.push(part);
        let item = std::fs::symlink_metadata(&current)
            .map_err(|error| format!("unsafe parent ancestor: {error}"))?;
        if !item.is_dir()
            || item.file_type().is_symlink()
            || (item.permissions().mode() & 0o022 != 0
                && !(item.uid() == 0 && item.permissions().mode() & 0o1000 != 0))
            || (item.uid() != 0 && item.uid() != uid_hint)
        {
            return Err("parent ancestor is writable or not a real directory".to_owned());
        }
    }
    Ok(())
}

fn owned_child_directory(parent: &Path) -> Result<OwnedRuntimeDirectory, String> {
    for _ in 0..8 {
        let name = format!("quota-inspector-{}", random_hex(16)?);
        let path = parent.join(name);
        let mut builder = std::fs::DirBuilder::new();
        builder.mode(0o700);
        match builder.create(&path) {
            Ok(()) => {
                let metadata = std::fs::symlink_metadata(&path)
                    .map_err(|error| format!("cannot verify private child directory: {error}"))?;
                if !metadata.is_dir()
                    || metadata.file_type().is_symlink()
                    || metadata.uid() != rustix::process::geteuid().as_raw()
                {
                    return Err("private child directory identity is unsafe".to_owned());
                }
                let owned = OwnedRuntimeDirectory {
                    path,
                    device: metadata.dev(),
                    inode: metadata.ino(),
                };
                let protect = (|| {
                    let descriptor = rustix::fs::open(
                        &owned.path,
                        rustix::fs::OFlags::RDONLY
                            | rustix::fs::OFlags::DIRECTORY
                            | rustix::fs::OFlags::NOFOLLOW
                            | rustix::fs::OFlags::CLOEXEC,
                        rustix::fs::Mode::empty(),
                    )
                    .map_err(|error| format!("cannot open private directory safely: {error}"))?;
                    let stat = rustix::fs::fstat(&descriptor)
                        .map_err(|error| format!("cannot verify private directory: {error}"))?;
                    if !rustix::fs::FileType::from_raw_mode(stat.st_mode).is_dir()
                        || (stat.st_dev, stat.st_ino) != (owned.device, owned.inode)
                    {
                        return Err("private directory changed during creation".to_owned());
                    }
                    rustix::fs::fchmod(&descriptor, rustix::fs::Mode::from_raw_mode(0o700))
                        .map_err(|error| {
                            format!("cannot protect private child directory: {error}")
                        })?;
                    let stat = rustix::fs::fstat(&descriptor)
                        .map_err(|error| format!("cannot verify protected directory: {error}"))?;
                    if rustix::fs::Mode::from_raw_mode(stat.st_mode).bits() & 0o777 != 0o700
                        || (stat.st_dev, stat.st_ino) != (owned.device, owned.inode)
                    {
                        return Err("protected child directory identity changed".to_owned());
                    }
                    Ok(())
                })();
                if let Err(error) = protect {
                    cleanup_owned(
                        &owned.path,
                        (owned.device, owned.inode),
                        &owned.path.join("inspector.sock"),
                        None,
                    );
                    return Err(error);
                }
                return Ok(owned);
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(format!("cannot create private child directory: {error}")),
        }
    }
    Err("cannot allocate unique private child directory".to_owned())
}

fn run_in_directory(
    socket_path: &Path,
    settings: &HarnessSettings<'_>,
    lifetime: Duration,
    socket_identity: &mut Option<(u64, u64)>,
) -> Result<(), String> {
    let address = rustix::net::SocketAddrUnix::new(socket_path)
        .map_err(|error| format!("cannot form socket address: {error}"))?;
    let socket = rustix::net::socket_with(
        rustix::net::AddressFamily::UNIX,
        rustix::net::SocketType::STREAM,
        rustix::net::SocketFlags::NONBLOCK,
        None,
    )
    .map_err(|error| format!("cannot create listener socket: {error}"))?;
    rustix::net::bind(&socket, &address).map_err(|error| format!("cannot bind socket: {error}"))?;
    let metadata = std::fs::symlink_metadata(socket_path)
        .map_err(|error| format!("cannot verify bound socket: {error}"))?;
    if !metadata.file_type().is_socket() || metadata.uid() != rustix::process::geteuid().as_raw() {
        return Err("bound socket identity is unsafe".to_owned());
    }
    *socket_identity = Some((metadata.dev(), metadata.ino()));
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o600))
        .map_err(|error| format!("cannot secure socket: {error}"))?;
    rustix::net::listen(&socket, 4).map_err(|error| format!("cannot listen on socket: {error}"))?;
    let listener = UnixListener::from(socket);
    let epoch = random_hex(16)?;
    let startup = serde_json::json!({
        "socket_path": socket_path,
        "inventory_generation": settings.inventory.generation,
        "helper_boot_epoch": epoch,
        "fixture_only": true
    });
    println!("{startup}");
    io::stdout()
        .flush()
        .map_err(|error| format!("cannot flush startup line: {error}"))?;
    let end = Instant::now() + lifetime;
    let mut replay = ReplayCache::default();
    let mut accepted: VecDeque<Instant> = VecDeque::new();
    while Instant::now() < end {
        match listener.accept() {
            Ok((mut stream, _)) => {
                let now = Instant::now();
                while accepted.front().is_some_and(|time| {
                    now.checked_duration_since(*time)
                        .is_some_and(|age| age >= Duration::from_secs(1))
                }) {
                    accepted.pop_front();
                }
                accepted.push_back(now);
                if accepted.len() > 10 {
                    continue;
                }
                let deadline = (now + CONNECTION_TIMEOUT).min(end);
                if serve_one(&mut stream, settings, &epoch, &mut replay, deadline).is_err() {
                    continue;
                }
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                let remaining = end.saturating_duration_since(Instant::now());
                std::thread::sleep(Duration::from_millis(5).min(remaining));
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(format!("listener accept failed: {error}")),
        }
    }
    Ok(())
}

fn serve_one(
    stream: &mut UnixStream,
    settings: &HarnessSettings<'_>,
    epoch: &str,
    replay: &mut ReplayCache,
    deadline: Instant,
) -> io::Result<()> {
    let credentials = peer_credentials(stream)?;
    if !check_peer(credentials, settings.allowed_uid, settings.allowed_gid) {
        return Ok(());
    }
    let payload = read_frame_before(stream, deadline)?;
    remaining(deadline)?;
    let request = match parse_request(&payload, settings.inventory) {
        Ok(request) => request,
        Err(_) => return Ok(()),
    };
    remaining(deadline)?;
    let response = match replay.check_and_insert(&request.challenge, Instant::now()) {
        ReplayDecision::New => settings
            .inventory
            .response(
                &request,
                epoch,
                unix_time_seconds().map_err(io::Error::other)?,
                settings.backend,
            )
            .map_err(io::Error::other)?,
        ReplayDecision::Replayed => error_response(
            &request,
            epoch,
            settings.inventory,
            "inconsistent",
            "challenge_replayed",
        ),
        ReplayDecision::Full => error_response(
            &request,
            epoch,
            settings.inventory,
            "inconsistent",
            "challenge_cache_full",
        ),
    };
    remaining(deadline)?;
    let frame = encode_frame(&response_json(&response).map_err(io::Error::other)?)?;
    write_before(stream, &frame, deadline)
}

fn error_response(
    request: &Request,
    epoch: &str,
    inventory: &Inventory,
    status: &str,
    code: &str,
) -> Response {
    Response {
        schema_version: 1,
        message_type: "storage_inspection_result".to_owned(),
        role_id: request.role_id.clone(),
        challenge: request.challenge.clone(),
        status: status.to_owned(),
        helper_boot_epoch: epoch.to_owned(),
        inventory_generation: inventory.generation.clone(),
        observed_at: None,
        filesystem_uuid: None,
        mount_target: None,
        mount_id: None,
        filesystem_type: None,
        quota_mode: None,
        quota_configuration_enabled: None,
        project_id: None,
        inheritance_enabled: None,
        byte_limit: None,
        inode_limit: None,
        bytes_used: None,
        inodes_used: None,
        filesystem_bytes: None,
        filesystem_bytes_available: None,
        filesystem_inodes: None,
        filesystem_inodes_available: None,
        owner_uid: None,
        owner_gid: None,
        error_code: Some(code.to_owned()),
    }
}

fn read_frame_before(stream: &mut UnixStream, deadline: Instant) -> io::Result<Vec<u8>> {
    let header = read_exact_before(stream, 4, deadline)?;
    let length = u32::from_be_bytes(header.try_into().expect("four-byte header")) as usize;
    if !(1..=MAX_FRAME_BYTES).contains(&length) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid frame length",
        ));
    }
    read_exact_before(stream, length, deadline)
}

fn read_exact_before(
    stream: &mut UnixStream,
    length: usize,
    deadline: Instant,
) -> io::Result<Vec<u8>> {
    let mut result = vec![0; length];
    let mut offset = 0;
    while offset < length {
        stream.set_read_timeout(Some(remaining(deadline)?))?;
        let count = stream.read(&mut result[offset..])?;
        if count == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "truncated frame",
            ));
        }
        offset += count;
    }
    Ok(result)
}

fn write_before(stream: &mut UnixStream, bytes: &[u8], deadline: Instant) -> io::Result<()> {
    let mut offset = 0;
    while offset < bytes.len() {
        stream.set_write_timeout(Some(remaining(deadline)?))?;
        let count = stream.write(&bytes[offset..])?;
        if count == 0 {
            return Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "socket write returned zero",
            ));
        }
        offset += count;
    }
    Ok(())
}

fn remaining(deadline: Instant) -> io::Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|time| !time.is_zero())
        .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "connection deadline elapsed"))
}

fn cleanup_owned(
    directory: &Path,
    directory_identity: (u64, u64),
    socket: &Path,
    socket_identity: Option<(u64, u64)>,
) {
    if !directory_matches(directory, directory_identity) {
        return;
    }
    if let Ok(metadata) = std::fs::symlink_metadata(socket)
        && metadata.file_type().is_socket()
        && metadata.uid() == rustix::process::geteuid().as_raw()
        && socket_identity == Some((metadata.dev(), metadata.ino()))
        && directory_matches(directory, directory_identity)
    {
        let _ = std::fs::remove_file(socket);
    }
    if directory_matches(directory, directory_identity) {
        let _ = std::fs::remove_dir(directory);
    }
}

fn directory_matches(directory: &Path, identity: (u64, u64)) -> bool {
    std::fs::symlink_metadata(directory).is_ok_and(|metadata| {
        metadata.is_dir()
            && !metadata.file_type().is_symlink()
            && metadata.uid() == rustix::process::geteuid().as_raw()
            && (metadata.dev(), metadata.ino()) == identity
    })
}

fn validate_role(role: &StorageRole) -> Result<(), String> {
    if !valid_role_id(&role.role_id)
        || role.kind != "task_slot"
        || !canonical_absolute_path(&role.path)
        || !canonical_absolute_path(&role.mount_path)
        || role.path == role.mount_path
        || !role.path.starts_with(&(role.mount_path.clone() + "/"))
        || role.mount_path == "/"
        || !valid_uuid(&role.filesystem_uuid)
        || role.project_id == 0
        || role.project_id > u32::MAX as u64
        || !(1024..=256 * 1024 * 1024).contains(&role.byte_limit)
        || !(16..=4096).contains(&role.inode_limit)
        || role.owner_uid > u32::MAX as u64
        || role.owner_gid > u32::MAX as u64
    {
        return Err(format!(
            "role {} violates synthetic fixed policy",
            role.role_id
        ));
    }
    Ok(())
}

fn validate_observation(role: &StorageRole, observation: &Observation) -> Result<(), String> {
    if observation.mount_id == 0
        || observation.filesystem_type != "ext4"
        || observation.quota_mode != "project"
        || !observation.quota_configuration_enabled
        || !observation.inheritance_enabled
        || observation.filesystem_uuid != role.filesystem_uuid
        || observation.mount_target != role.mount_path
        || observation.project_id != role.project_id
        || observation.byte_limit != role.byte_limit
        || observation.inode_limit != role.inode_limit
        || observation.owner_uid != role.owner_uid
        || observation.owner_gid != role.owner_gid
        || observation.bytes_used > observation.byte_limit
        || observation.inodes_used > observation.inode_limit
        || observation.filesystem_bytes_available > observation.filesystem_bytes
        || observation.filesystem_inodes_available > observation.filesystem_inodes
    {
        return Err(format!("observation for {} is inconsistent", role.role_id));
    }
    Ok(())
}

fn canonical_absolute_path(value: &str) -> bool {
    value.starts_with('/')
        && value.len() > 1
        && !value.ends_with('/')
        && !value.split('/').any(|part| part == "." || part == "..")
        && !value.contains("//")
        && !value.contains('\0')
}

fn valid_uuid(value: &str) -> bool {
    value.len() == 36
        && value.as_bytes().iter().enumerate().all(|(index, byte)| {
            if [8, 13, 18, 23].contains(&index) {
                *byte == b'-'
            } else {
                byte.is_ascii_digit()
                    || (b'a'..=b'f').contains(byte)
                    || (b'A'..=b'F').contains(byte)
            }
        })
}

fn valid_role_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 64
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'_' || *byte == b'-'
        })
}

fn valid_hex(value: &str, exact_len: usize) -> bool {
    value.len() == exact_len
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn check_json_depth(value: &str, limit: usize) -> Result<(), String> {
    let mut depth = 0_usize;
    let mut in_string = false;
    let mut escaped = false;
    for byte in value.bytes() {
        if in_string {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                in_string = false;
            }
            continue;
        }
        match byte {
            b'"' => in_string = true,
            b'{' | b'[' => {
                depth += 1;
                if depth > limit {
                    return Err("JSON nesting exceeds fixed parser limit".to_owned());
                }
            }
            b'}' | b']' => depth = depth.saturating_sub(1),
            _ => {}
        }
    }
    Ok(())
}

use std::os::unix::fs::MetadataExt;

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture_inventory() -> Inventory {
        Inventory::from_json(include_str!(
            "../../../tests/fixtures/quota_inspector_v1/inventory.json"
        ))
        .expect("shared golden inventory")
    }

    #[test]
    fn shared_inventory_generation_and_observation_are_exact() {
        let inventory = fixture_inventory();
        let disk_inventory = Inventory::load(
            &Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../tests/fixtures/quota_inspector_v1/inventory.json"),
        )
        .expect("bounded no-follow inventory file");
        assert_eq!(disk_inventory.generation, inventory.generation);
        assert_eq!(
            inventory.generation,
            "dceeb2679fd90065e786f689b67ca00545c8f50bb5158f7043d03b9812a6d609"
        );
        assert_eq!(inventory.roles.len(), 1);
        let exchanges: serde_json::Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/quota_inspector_v1/exchanges.json"
        ))
        .expect("exchanges JSON");
        let request: Request =
            serde_json::from_value(exchanges["request"].clone()).expect("request fixture");
        let response = inventory
            .response(
                &request,
                "abcdef0123456789abcdef0123456789",
                1_700_000_000,
                Backend::Fixture,
            )
            .expect("fixture response");
        let actual: serde_json::Value =
            serde_json::from_slice(&response_json(&response).expect("serialize"))
                .expect("response JSON");
        let expected = serde_json::from_str::<serde_json::Value>(include_str!(
            "../../../tests/fixtures/quota_inspector_v1/exchanges.json"
        ))
        .expect("exchanges")["response"]
            .clone();
        assert_eq!(actual, expected);
        assert_eq!(
            parse_response(
                &response_json(&response).expect("serialize"),
                &request,
                &inventory,
                1_700_000_000,
                5,
            ),
            Ok(response)
        );
    }

    #[test]
    fn shared_invalid_response_mutations_match_client_error_categories() {
        let inventory = fixture_inventory();
        let exchanges: serde_json::Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/quota_inspector_v1/exchanges.json"
        ))
        .expect("exchanges");
        let request: Request =
            serde_json::from_value(exchanges["request"].clone()).expect("request");
        for mutation in exchanges["invalid_responses"]
            .as_array()
            .expect("mutations")
        {
            let mut candidate = exchanges["response"].clone();
            if let Some(changes) = mutation["changes"].as_object() {
                for (key, value) in changes {
                    candidate[key] = value.clone();
                }
            }
            if let Some(key) = mutation["remove"].as_str() {
                candidate
                    .as_object_mut()
                    .expect("response object")
                    .remove(key);
            }
            let result = parse_response(
                candidate.to_string().as_bytes(),
                &request,
                &inventory,
                1_700_000_000,
                5,
            );
            let expected = match mutation["error"].as_str().expect("error") {
                "invalid" => ResponseValidationError::Invalid,
                "stale" => ResponseValidationError::Stale,
                "inconsistent" => ResponseValidationError::Inconsistent,
                unexpected => panic!("unexpected fixture error category: {unexpected}"),
            };
            assert_eq!(
                result.unwrap_err(),
                expected,
                "mutation {}",
                mutation["name"]
            );
        }
    }

    #[test]
    fn inventory_generation_uses_python_ascii_escaping_and_role_sorting() {
        let role = StorageRole {
            role_id: "fixture-role".to_owned(),
            kind: "task_slot".to_owned(),
            path: "/synthetic/é/😀".to_owned(),
            mount_path: "/synthetic/é".to_owned(),
            filesystem_uuid: "11111111-2222-4333-8444-555555555555".to_owned(),
            project_id: 4242,
            byte_limit: 1_048_576,
            inode_limit: 128,
            owner_uid: 4242,
            owner_gid: 4242,
        };
        assert_eq!(
            inventory_generation(std::slice::from_ref(&role)),
            "1302af88acf5a757ee49e1404df150434035d41fb84d40d599aab369ad152f5d"
        );
        let mut second = role.clone();
        second.role_id = "another-role".to_owned();
        assert_eq!(
            inventory_generation(&[role.clone(), second.clone()]),
            inventory_generation(&[second, role.clone()])
        );
        assert_eq!(python_json_string("é😀"), "\"\\u00e9\\ud83d\\ude00\"");
    }

    #[test]
    fn strict_request_parser_rejects_duplicates_unknowns_missing_and_bad_types() {
        let inventory = fixture_inventory();
        let root: serde_json::Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/quota_inspector_v1/exchanges.json"
        ))
        .expect("json");
        let request = root["request"].to_string();
        assert!(parse_request(request.as_bytes(), &inventory).is_ok());
        for bad in [
            r#"{"schema_version":1,"schema_version":1}"#,
            r#"{"schema_version":1,"message_type":"inspect_storage_role","role_id":"fixture-role","challenge":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","inventory_generation":"a8fa093f65491852f07a6221bab3018b600256d4c51cc7bc2d04cc48f7a8304c","extra":1}"#,
            r#"{"schema_version":true,"message_type":"inspect_storage_role","role_id":"fixture-role","challenge":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","inventory_generation":"a8fa093f65491852f07a6221bab3018b600256d4c51cc7bc2d04cc48f7a8304c"}"#,
            r#"{"schema_version":1,"message_type":"inspect_storage_role","role_id":"fixture-role","challenge":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","inventory_generation":"a8fa093f65491852f07a6221bab3018b600256d4c51cc7bc2d04cc48f7a8304c","path":"/etc"}"#,
        ] {
            assert!(parse_request(bad.as_bytes(), &inventory).is_err(), "{bad}");
        }
        for mutation in root["invalid_requests"].as_array().expect("mutations") {
            let mut candidate = root["request"].clone();
            if let Some(changes) = mutation["changes"].as_object() {
                for (key, value) in changes {
                    candidate[key] = value.clone();
                }
            }
            if let Some(key) = mutation["remove"].as_str() {
                candidate
                    .as_object_mut()
                    .expect("request object")
                    .remove(key);
            }
            assert!(
                parse_request(candidate.to_string().as_bytes(), &inventory).is_err(),
                "invalid request mutation {} was accepted",
                mutation["name"]
            );
        }
    }

    #[test]
    fn parser_bounds_utf8_lengths_and_nesting() {
        let inventory = fixture_inventory();
        assert!(parse_request(&[0xff], &inventory).is_err());
        assert!(parse_request(&vec![b' '; MAX_FRAME_BYTES + 1], &inventory).is_err());
        assert!(
            check_json_depth(
                &format!("{}0{}", "[".repeat(40), "]".repeat(40)),
                MAX_JSON_DEPTH
            )
            .is_err()
        );
        assert!(check_json_depth(r#"{"x":"[[[[[[[[[[[[[[[[[[[[["}"#, MAX_JSON_DEPTH).is_ok());
    }

    #[test]
    fn framing_is_big_endian_and_enforces_bounds() {
        assert_eq!(encode_frame(b"abc").expect("frame"), b"\0\0\0\x03abc");
        assert!(encode_frame(&[]).is_err());
        assert!(encode_frame(&vec![0; MAX_FRAME_BYTES + 1]).is_err());
        assert!(decode_frame(&mut &b"\0\0\x40\x01"[..]).is_err());
    }

    #[test]
    fn response_backend_unavailable_never_fabricates_observation() {
        let inventory = fixture_inventory();
        let request = Request {
            schema_version: 1,
            message_type: "inspect_storage_role".to_owned(),
            role_id: "fixture-role".to_owned(),
            challenge: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                .to_owned(),
            inventory_generation: inventory.generation.clone(),
        };
        let result = inventory
            .response(
                &request,
                "abcdef0123456789abcdef0123456789",
                1,
                Backend::NativeUnavailable,
            )
            .expect("unavailable");
        assert_eq!(result.status, "unavailable");
        assert_eq!(
            result.error_code.as_deref(),
            Some("native_backend_unavailable")
        );
        assert!(result.observed_at.is_none());
        let exchanges: serde_json::Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/quota_inspector_v1/exchanges.json"
        ))
        .expect("exchanges");
        let expected = exchanges["unavailable"].clone();
        let actual: serde_json::Value =
            serde_json::from_slice(&response_json(&result).expect("serialize")).expect("response");
        assert_eq!(actual, expected);
    }

    #[test]
    fn replay_cache_expires_at_ttl_and_never_evicts_live_entries() {
        let mut cache = ReplayCache::default();
        let now = Instant::now();
        for value in 0..REPLAY_CAPACITY {
            assert_eq!(
                cache.check_and_insert(&value.to_string(), now),
                ReplayDecision::New
            );
        }
        assert_eq!(
            cache.check_and_insert("overflow", now),
            ReplayDecision::Full
        );
        assert_eq!(cache.len(), REPLAY_CAPACITY);
        assert_eq!(cache.check_and_insert("0", now), ReplayDecision::Replayed);
        assert_eq!(
            cache.check_and_insert("after-expiry", now + REPLAY_TTL),
            ReplayDecision::New
        );
        assert_eq!(cache.len(), 1);
    }

    #[test]
    fn exact_peer_authorization_checks_pid_uid_and_gid() {
        assert!(check_peer(
            PeerCredentials {
                pid: 1,
                uid: 42,
                gid: 43
            },
            42,
            43
        ));
        assert!(!check_peer(
            PeerCredentials {
                pid: 0,
                uid: 42,
                gid: 43
            },
            42,
            43
        ));
        assert!(!check_peer(
            PeerCredentials {
                pid: 1,
                uid: 41,
                gid: 43
            },
            42,
            43
        ));
        assert!(!check_peer(
            PeerCredentials {
                pid: 1,
                uid: 42,
                gid: 44
            },
            42,
            43
        ));
    }

    #[test]
    fn same_user_unix_peer_credentials_are_read_from_the_kernel() {
        let (server, _client) = UnixStream::pair().expect("Unix socket pair");
        let credentials = peer_credentials(&server).expect("SO_PEERCRED");
        assert!(check_peer(
            credentials,
            rustix::process::geteuid().as_raw(),
            rustix::process::getegid().as_raw()
        ));
    }

    #[test]
    fn malformed_inventory_shape_and_generation_are_refused() {
        let original = include_str!("../../../tests/fixtures/quota_inspector_v1/inventory.json");
        assert!(Inventory::from_json(&original.replace("1048576", "1")).is_err());
        assert!(
            Inventory::from_json(
                &original.replace("\"kind\": \"task_slot\"", "\"kind\": \"podman_graph\"")
            )
            .is_err()
        );
        assert!(
            Inventory::from_json(&original.replace(
                "\"label\": \"synthetic_offline_only\"",
                "\"label\": \"other\""
            ))
            .is_err()
        );
        assert!(
            Inventory::from_json(
                &original.replace("\"owner_gid\": 4242", "\"owner_gid\": 18446744073709551616")
            )
            .is_err()
        );
    }

    #[test]
    fn duplicate_inventory_keys_are_rejected() {
        let original = include_str!("../../../tests/fixtures/quota_inspector_v1/inventory.json");
        let duplicate = original.replacen(
            "\"label\": \"synthetic_offline_only\"",
            "\"label\": \"synthetic_offline_only\", \"label\": \"synthetic_offline_only\"",
            1,
        );
        assert!(Inventory::from_json(&duplicate).is_err());
        let duplicate_observation = original.replacen(
            "\"observations\": {\n    \"fixture-role\": {",
            "\"observations\": {\n    \"fixture-role\": {},\n    \"fixture-role\": {",
            1,
        );
        assert!(Inventory::from_json(&duplicate_observation).is_err());
    }
}
