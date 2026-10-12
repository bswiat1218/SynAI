use crate::AbiError;

/// A syntactically representable ID, not an approved project assignment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProjectId(u32);

impl ProjectId {
    pub fn new(value: u64) -> Result<Self, AbiError> {
        let value = u32::try_from(value).map_err(|_| AbiError::InvalidProjectId)?;
        if value == 0 || value == u32::MAX {
            return Err(AbiError::InvalidProjectId);
        }
        Ok(Self(value))
    }

    pub fn value(self) -> u32 {
        self.0
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadCommand {
    GetQuota(ProjectId),
    GetInfo,
}

impl ReadCommand {
    /// Exact unsigned QCMD encoding. This does not invoke or authorize a query.
    pub fn encoded(self) -> Result<u32, AbiError> {
        let command: u32 = match self {
            Self::GetQuota(_) => 0x800007,
            Self::GetInfo => 0x800005,
        };
        command
            .checked_mul(256)
            .map(|value| value | 2)
            .ok_or(AbiError::Overflow)
    }

    /// GETINFO has no per-project selection; its reserved ID is fixed to zero.
    pub fn id_argument(self) -> u32 {
        match self {
            Self::GetQuota(project) => project.value(),
            Self::GetInfo => 0,
        }
    }

    pub fn buffer_size(self) -> usize {
        match self {
            Self::GetQuota(_) => 72,
            Self::GetInfo => 24,
        }
    }
}
