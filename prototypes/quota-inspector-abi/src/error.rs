#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AbiError {
    InvalidProjectId,
    BufferSize,
    MissingValidity,
    UnknownValidity,
    UnknownFlags,
    Overflow,
    UnlimitedHardLimit,
    ContradictoryLimits,
}

impl std::fmt::Display for AbiError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::InvalidProjectId => "invalid_project_id",
            Self::BufferSize => "invalid_buffer_size",
            Self::MissingValidity => "missing_validity",
            Self::UnknownValidity => "unknown_validity",
            Self::UnknownFlags => "unknown_flags",
            Self::Overflow => "counter_overflow",
            Self::UnlimitedHardLimit => "unlimited_hard_limit",
            Self::ContradictoryLimits => "contradictory_limits",
        })
    }
}

impl std::error::Error for AbiError {}

/// Classification of positive libc errno, not a kernel return-value decoder.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrnoClass {
    UnsupportedInterface,
    UnsupportedOperation,
    PermissionDenied,
    InvalidDescriptor,
    InvalidArgument,
    MissingRecord,
    Interrupted,
    IoFailure,
    BadBuffer,
    Cancelled,
    TimedOut,
    Unknown(i32),
}

pub fn classify_errno(errno: i32) -> ErrnoClass {
    match errno {
        38 => ErrnoClass::UnsupportedInterface,
        95 => ErrnoClass::UnsupportedOperation,
        1 | 13 => ErrnoClass::PermissionDenied,
        9 => ErrnoClass::InvalidDescriptor,
        22 => ErrnoClass::InvalidArgument,
        2 | 3 | 61 => ErrnoClass::MissingRecord,
        4 => ErrnoClass::Interrupted,
        5 => ErrnoClass::IoFailure,
        14 => ErrnoClass::BadBuffer,
        125 => ErrnoClass::Cancelled,
        110 => ErrnoClass::TimedOut,
        value => ErrnoClass::Unknown(value),
    }
}
