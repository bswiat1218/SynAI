#![forbid(unsafe_code)]

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

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

pub fn python_json_string(value: &str) -> String {
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
            '\u{7f}' => result.push_str("\\u007f"),
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
