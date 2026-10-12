use crate::{AbiError, InfoRecord, QuotaRecord};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ByteLimit {
    Unlimited,
    Bytes(u64),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InodeLimit {
    Unlimited,
    Inodes(u64),
}

/// Caller-supplied decoded data, never inventory authority or an observation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct QuotaCandidate {
    pub hard_bytes: ByteLimit,
    pub soft_bytes: ByteLimit,
    pub used_bytes: u64,
    pub hard_inodes: InodeLimit,
    pub soft_inodes: InodeLimit,
    pub used_inodes: u64,
    /// Unix expiry timestamps, not durations; None means validity was absent.
    pub byte_expiry: Option<u64>,
    pub inode_expiry: Option<u64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InfoCandidate {
    pub byte_grace_seconds: u64,
    pub inode_grace_seconds: u64,
    pub root_squash: bool,
    pub system_file: bool,
}

fn bytes(blocks: u64) -> Result<ByteLimit, AbiError> {
    if blocks == 0 {
        return Ok(ByteLimit::Unlimited);
    }
    blocks
        .checked_mul(1024)
        .map(ByteLimit::Bytes)
        .ok_or(AbiError::Overflow)
}

fn inodes(value: u64) -> InodeLimit {
    if value == 0 {
        InodeLimit::Unlimited
    } else {
        InodeLimit::Inodes(value)
    }
}

impl QuotaRecord {
    pub fn decode(self) -> Result<QuotaCandidate, AbiError> {
        if self.valid & !0x3f != 0 {
            return Err(AbiError::UnknownValidity);
        }
        if self.valid & 0x0f != 0x0f {
            return Err(AbiError::MissingValidity);
        }
        if self.hard_blocks != 0 && self.soft_blocks > self.hard_blocks
            || self.hard_inodes != 0 && self.soft_inodes > self.hard_inodes
        {
            return Err(AbiError::ContradictoryLimits);
        }
        Ok(QuotaCandidate {
            hard_bytes: bytes(self.hard_blocks)?,
            soft_bytes: bytes(self.soft_blocks)?,
            used_bytes: self.used_bytes,
            hard_inodes: inodes(self.hard_inodes),
            soft_inodes: inodes(self.soft_inodes),
            used_inodes: self.used_inodes,
            byte_expiry: (self.valid & 0x10 != 0).then_some(self.byte_expiry),
            inode_expiry: (self.valid & 0x20 != 0).then_some(self.inode_expiry),
        })
    }
}

impl QuotaCandidate {
    /// Representation check only. Approval, exact ceilings and enforcement remain external.
    pub fn positive_hard_caps(self) -> Result<(u64, u64), AbiError> {
        match (self.hard_bytes, self.hard_inodes) {
            (ByteLimit::Bytes(bytes), InodeLimit::Inodes(inodes)) if bytes > 0 && inodes > 0 => {
                Ok((bytes, inodes))
            }
            _ => Err(AbiError::UnlimitedHardLimit),
        }
    }
}

impl InfoRecord {
    pub fn decode(self) -> Result<InfoCandidate, AbiError> {
        if self.valid & !7 != 0 {
            return Err(AbiError::UnknownValidity);
        }
        if self.valid & 7 != 7 {
            return Err(AbiError::MissingValidity);
        }
        if self.flags & !0x10001 != 0 {
            return Err(AbiError::UnknownFlags);
        }
        Ok(InfoCandidate {
            byte_grace_seconds: self.byte_grace_seconds,
            inode_grace_seconds: self.inode_grace_seconds,
            root_squash: self.flags & 1 != 0,
            system_file: self.flags & 0x10000 != 0,
        })
    }
}
