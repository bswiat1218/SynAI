#![forbid(unsafe_code)]

//! Pure, untrusted Linux quota ABI decoding. No transport or syscall invoker exists.
//!
//! Unsupported commands and synthetic transports are not public APIs:
//! ```compile_fail
//! use quota_inspector_abi::ReadCommand;
//! let command = ReadCommand::SetQuota;
//! ```
//! ```compile_fail
//! use quota_inspector_abi::ReadCommand;
//! let command = ReadCommand::from_raw(0x80000802);
//! ```
//! ```compile_fail
//! use quota_inspector_abi::tests::FakeTransport;
//! ```
//! ```compile_fail
//! use quota_inspector_abi::invoke_quotactl_fd;
//! ```
//! ```compile_fail
//! use quota_inspector_abi::ProjectId;
//! let id = ProjectId(4242);
//! ```
//! ```compile_fail
//! use quota_inspector_abi::ReadCommand;
//! let command = ReadCommand::SetInfo;
//! ```
//! ```compile_fail
//! use quota_inspector_abi::ReadCommand;
//! let command = ReadCommand::QuotaOn;
//! ```
//! ```compile_fail
//! use quota_inspector_abi::ReadCommand;
//! let command = ReadCommand::QuotaOff;
//! ```
//! ```compile_fail
//! use quota_inspector_abi::ReadCommand;
//! let command = ReadCommand::Sync;
//! ```
//! ```compile_fail
//! use quota_inspector_abi::ReadCommand;
//! let command = ReadCommand::GetNextQuota;
//! ```
//! ```compile_fail
//! use quota_inspector_abi::ReadCommand;
//! let command = ReadCommand::GetFormat;
//! ```
//! ```compile_fail
//! use quota_inspector_abi::quotactl;
//! ```
//! ```compile_fail
//! use quota_inspector_abi::ioctl;
//! ```
//! An acquisition cannot outlive its descriptor owner:
//! ```compile_fail
//! use std::os::fd::AsFd;
//! use quota_inspector_abi::{Acquisition, ReadCommand};
//! let acquisition = {
//!     let file = std::fs::File::open("Cargo.toml").unwrap();
//!     Acquisition::new(file.as_fd(), ReadCommand::GetInfo)
//! };
//! let _ = acquisition.descriptor();
//! ```
//! ```compile_fail
//! use quota_inspector_abi::Acquisition;
//! let _ = Acquisition::from_path("/host/device");
//! ```

#[cfg(not(all(
    target_os = "linux",
    target_arch = "x86_64",
    target_env = "gnu",
    target_pointer_width = "64",
    target_endian = "little"
)))]
compile_error!("quota-inspector-abi supports only x86_64 Linux GNU LP64 little-endian");

mod acquisition;
mod command;
mod decode;
mod error;
mod layout;
mod target;

pub use acquisition::{
    Acquisition, AcquisitionError, DecodedCandidate, KernelSignature, OutputBuffer, ReportedStatus,
};
pub use command::{ProjectId, ReadCommand};
pub use decode::{ByteLimit, InfoCandidate, InodeLimit, QuotaCandidate};
pub use error::{AbiError, ErrnoClass, classify_errno};
pub use layout::{InfoRecord, QuotaRecord};
pub use target::{SUPPORTED_TARGET, SYSCALL_NUMBER};

#[cfg(test)]
mod tests;
