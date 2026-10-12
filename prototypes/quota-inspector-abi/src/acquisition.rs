use std::os::fd::BorrowedFd;

use crate::{
    AbiError, ErrnoClass, InfoCandidate, InfoRecord, QuotaCandidate, QuotaRecord, ReadCommand,
    classify_errno,
};

/// Calling-convention shape only: no FFI declaration or function with this body exists.
pub type KernelSignature = extern "C" fn(u32, u32, u32, *mut std::ffi::c_void) -> std::ffi::c_long;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AcquisitionError {
    Errno(ErrnoClass),
    InvalidReturn,
    InvalidErrno,
    OutputKind,
    Decode(AbiError),
}

/// A reported status, not proof that physical work stopped or a descriptor was qualified.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReportedStatus;

impl ReportedStatus {
    /// Linux raw negative errno conventions. This quota interface succeeds only with zero.
    pub fn from_kernel(value: std::ffi::c_long) -> Result<Self, AcquisitionError> {
        if value == 0 {
            Ok(Self)
        } else if (-4095..=-1).contains(&value) {
            let errno = i32::try_from(-value).map_err(|_| AcquisitionError::InvalidErrno)?;
            Err(AcquisitionError::Errno(classify_errno(errno)))
        } else {
            Err(AcquisitionError::InvalidReturn)
        }
    }

    /// GNU libc reports -1 and positive errno, not a raw negative kernel errno.
    pub fn from_libc(value: std::ffi::c_long, errno: i32) -> Result<Self, AcquisitionError> {
        match value {
            0 => Ok(Self), // errno may be stale after a successful call.
            -1 if (1..=4095).contains(&errno) => {
                Err(AcquisitionError::Errno(classify_errno(errno)))
            }
            -1 => Err(AcquisitionError::InvalidErrno),
            _ => Err(AcquisitionError::InvalidReturn),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OutputBuffer {
    Quota([u8; QuotaRecord::SIZE]),
    Info([u8; InfoRecord::SIZE]),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DecodedCandidate {
    Quota(QuotaCandidate),
    Info(InfoCandidate),
}

/// Borrows an already-owned FD; neither its validity nor approval is established here.
/// Any asynchronous adapter must independently retain the owning lease until safe completion.
pub struct Acquisition<'fd> {
    descriptor: BorrowedFd<'fd>,
    command: ReadCommand,
}

impl<'fd> Acquisition<'fd> {
    pub fn new(descriptor: BorrowedFd<'fd>, command: ReadCommand) -> Self {
        Self {
            descriptor,
            command,
        }
    }

    pub fn descriptor(&self) -> BorrowedFd<'_> {
        self.descriptor
    }

    pub fn command(&self) -> ReadCommand {
        self.command
    }

    pub fn initialized_output(&self) -> OutputBuffer {
        match self.command {
            ReadCommand::GetQuota(_) => OutputBuffer::Quota(QuotaRecord::initialized_buffer()),
            ReadCommand::GetInfo => OutputBuffer::Info(InfoRecord::initialized_buffer()),
        }
    }

    /// Pure decoding of caller-reported output. Never a completion/ownership/authority token.
    pub fn decode_reported_output(
        &self,
        status: Result<ReportedStatus, AcquisitionError>,
        output: OutputBuffer,
    ) -> Result<DecodedCandidate, AcquisitionError> {
        status?;
        match (self.command, output) {
            (ReadCommand::GetQuota(_), OutputBuffer::Quota(bytes)) => {
                QuotaRecord::from_bytes(&bytes)
                    .and_then(QuotaRecord::decode)
                    .map(DecodedCandidate::Quota)
                    .map_err(AcquisitionError::Decode)
            }
            (ReadCommand::GetInfo, OutputBuffer::Info(bytes)) => InfoRecord::from_bytes(&bytes)
                .and_then(InfoRecord::decode)
                .map(DecodedCandidate::Info)
                .map_err(AcquisitionError::Decode),
            _ => Err(AcquisitionError::OutputKind),
        }
    }
}
