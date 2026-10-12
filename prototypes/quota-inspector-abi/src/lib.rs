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

#[cfg(not(all(
    target_os = "linux",
    target_arch = "x86_64",
    target_env = "gnu",
    target_pointer_width = "64",
    target_endian = "little"
)))]
compile_error!("quota-inspector-abi supports only x86_64 Linux GNU LP64 little-endian");

mod command;
mod decode;
mod error;
mod layout;
mod target;

pub use command::{ProjectId, ReadCommand};
pub use decode::{ByteLimit, InfoCandidate, InodeLimit, QuotaCandidate};
pub use error::{AbiError, ErrnoClass, classify_errno};
pub use layout::{InfoRecord, QuotaRecord};
pub use target::{SUPPORTED_TARGET, SYSCALL_NUMBER};

#[cfg(test)]
mod tests;
