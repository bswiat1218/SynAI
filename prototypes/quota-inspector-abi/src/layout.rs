use crate::AbiError;

/// Initialized LP64 if_dqblk representation. No pointer or syscall access is exposed.
#[repr(C)]
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct QuotaRecord {
    pub(crate) hard_blocks: u64,
    pub(crate) soft_blocks: u64,
    pub(crate) used_bytes: u64,
    pub(crate) hard_inodes: u64,
    pub(crate) soft_inodes: u64,
    pub(crate) used_inodes: u64,
    pub(crate) byte_expiry: u64,
    pub(crate) inode_expiry: u64,
    pub(crate) valid: u32,
    reserved: [u8; 4],
}

#[repr(C)]
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct InfoRecord {
    pub(crate) byte_grace_seconds: u64,
    pub(crate) inode_grace_seconds: u64,
    pub(crate) flags: u32,
    pub(crate) valid: u32,
}

fn word(bytes: &[u8], offset: usize) -> u64 {
    u64::from_le_bytes(
        bytes[offset..offset + 8]
            .try_into()
            .expect("checked buffer"),
    )
}

impl QuotaRecord {
    pub const SIZE: usize = 72;

    pub fn initialized_buffer() -> [u8; Self::SIZE] {
        [0; Self::SIZE]
    }

    pub fn from_bytes(bytes: &[u8]) -> Result<Self, AbiError> {
        if bytes.len() != Self::SIZE {
            return Err(AbiError::BufferSize);
        }
        Ok(Self {
            hard_blocks: word(bytes, 0),
            soft_blocks: word(bytes, 8),
            used_bytes: word(bytes, 16),
            hard_inodes: word(bytes, 24),
            soft_inodes: word(bytes, 32),
            used_inodes: word(bytes, 40),
            byte_expiry: word(bytes, 48),
            inode_expiry: word(bytes, 56),
            valid: u32::from_le_bytes(bytes[64..68].try_into().expect("checked buffer")),
            // Kernel tail padding is never interpreted or copied into evidence.
            reserved: [0; 4],
        })
    }
}

impl InfoRecord {
    pub const SIZE: usize = 24;

    pub fn initialized_buffer() -> [u8; Self::SIZE] {
        [0; Self::SIZE]
    }

    pub fn from_bytes(bytes: &[u8]) -> Result<Self, AbiError> {
        if bytes.len() != Self::SIZE {
            return Err(AbiError::BufferSize);
        }
        Ok(Self {
            byte_grace_seconds: word(bytes, 0),
            inode_grace_seconds: word(bytes, 8),
            flags: u32::from_le_bytes(bytes[16..20].try_into().expect("checked buffer")),
            valid: u32::from_le_bytes(bytes[20..24].try_into().expect("checked buffer")),
        })
    }
}

const _: () = {
    assert!(size_of::<QuotaRecord>() == 72);
    assert!(align_of::<QuotaRecord>() == 8);
    assert!(std::mem::offset_of!(QuotaRecord, hard_blocks) == 0);
    assert!(std::mem::offset_of!(QuotaRecord, soft_blocks) == 8);
    assert!(std::mem::offset_of!(QuotaRecord, used_bytes) == 16);
    assert!(std::mem::offset_of!(QuotaRecord, hard_inodes) == 24);
    assert!(std::mem::offset_of!(QuotaRecord, soft_inodes) == 32);
    assert!(std::mem::offset_of!(QuotaRecord, used_inodes) == 40);
    assert!(std::mem::offset_of!(QuotaRecord, byte_expiry) == 48);
    assert!(std::mem::offset_of!(QuotaRecord, inode_expiry) == 56);
    assert!(std::mem::offset_of!(QuotaRecord, valid) == 64);
    assert!(std::mem::offset_of!(QuotaRecord, reserved) == 68);
    assert!(size_of::<InfoRecord>() == 24);
    assert!(align_of::<InfoRecord>() == 8);
    assert!(std::mem::offset_of!(InfoRecord, byte_grace_seconds) == 0);
    assert!(std::mem::offset_of!(InfoRecord, inode_grace_seconds) == 8);
    assert!(std::mem::offset_of!(InfoRecord, flags) == 16);
    assert!(std::mem::offset_of!(InfoRecord, valid) == 20);
};
