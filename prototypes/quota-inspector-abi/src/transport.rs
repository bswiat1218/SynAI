#![allow(unsafe_code)]

#[cfg(not(test))]
use std::ffi::c_int;
#[cfg(not(test))]
use std::ffi::{c_long, c_void};
use std::os::fd::AsRawFd;

#[cfg(not(test))]
use crate::SYSCALL_NUMBER;
use crate::{Acquisition, AcquisitionError, DecodedCandidate, OutputBuffer, ReportedStatus};

#[cfg(not(test))]
unsafe extern "C" {
    fn syscall(number: c_long, ...) -> c_long;
    fn __errno_location() -> *mut c_int;
}

pub(crate) trait KernelDispatcher {
    fn invoke(
        &mut self,
        fd: u32,
        command: u32,
        id: u32,
        output: &mut [u8],
    ) -> Result<ReportedStatus, AcquisitionError>;
}

#[derive(Default)]
#[cfg(not(test))]
struct LibcDispatcher;

#[cfg(not(test))]
impl KernelDispatcher for LibcDispatcher {
    fn invoke(
        &mut self,
        fd: u32,
        command: u32,
        id: u32,
        output: &mut [u8],
    ) -> Result<ReportedStatus, AcquisitionError> {
        // SAFETY: We pass the reviewed syscall number and exact fixed-width ABI
        // arguments for fd/cmd/id plus a writable output pointer to a fixed buffer.
        let raw = unsafe {
            syscall(
                c_long::from(SYSCALL_NUMBER),
                c_long::from(fd),
                c_long::from(command),
                c_long::from(id),
                output.as_mut_ptr().cast::<c_void>(),
            )
        };
        let errno = if raw == -1 {
            // SAFETY: `__errno_location` returns a valid thread-local errno pointer.
            unsafe { *__errno_location() }
        } else {
            0
        };
        ReportedStatus::from_libc(raw, errno)
    }
}

/// Executes the fixed Linux `quotactl_fd` read command encoded in `acquisition`.
///
/// This enters the kernel on supported non-test builds. The borrowed descriptor
/// and project binding must already be independently authorized. Decoded data is
/// untrusted evidence and never establishes enforcement or Broker readiness.
pub fn invoke_quotactl_fd(
    acquisition: &Acquisition<'_>,
) -> Result<DecodedCandidate, AcquisitionError> {
    #[cfg(test)]
    {
        let _ = acquisition;
        panic!("real quotactl_fd transport is disabled in tests; use test dispatcher injection");
    }
    #[cfg(not(test))]
    {
        let mut dispatcher = LibcDispatcher;
        invoke_quotactl_fd_with(acquisition, &mut dispatcher)
    }
}

pub(crate) fn invoke_quotactl_fd_with(
    acquisition: &Acquisition<'_>,
    dispatcher: &mut dyn KernelDispatcher,
) -> Result<DecodedCandidate, AcquisitionError> {
    let raw_fd = acquisition.descriptor().as_raw_fd();
    let fd = u32::try_from(raw_fd).map_err(|_| AcquisitionError::InvalidReturn)?;
    let mut output = aligned_output(acquisition.command());
    let encoded = acquisition
        .command()
        .encoded()
        .map_err(AcquisitionError::Decode)?;
    let status = dispatcher.invoke(
        fd,
        encoded,
        acquisition.command().id_argument(),
        output_bytes_mut(&mut output),
    );
    acquisition.decode_reported_output(status, finish_output(acquisition.command(), output))
}

fn output_bytes_mut(output: &mut AlignedOutput) -> &mut [u8] {
    match output {
        AlignedOutput::Quota(bytes) => &mut bytes.0,
        AlignedOutput::Info(bytes) => &mut bytes.0,
    }
}

#[repr(align(8))]
struct AlignedQuota([u8; 72]);

#[repr(align(8))]
struct AlignedInfo([u8; 24]);

enum AlignedOutput {
    Quota(AlignedQuota),
    Info(AlignedInfo),
}

fn aligned_output(command: crate::ReadCommand) -> AlignedOutput {
    match command {
        crate::ReadCommand::GetQuota(_) => AlignedOutput::Quota(AlignedQuota([0; 72])),
        crate::ReadCommand::GetInfo => AlignedOutput::Info(AlignedInfo([0; 24])),
    }
}

fn finish_output(command: crate::ReadCommand, output: AlignedOutput) -> OutputBuffer {
    match (command, output) {
        (crate::ReadCommand::GetQuota(_), AlignedOutput::Quota(bytes)) => {
            OutputBuffer::Quota(bytes.0)
        }
        (crate::ReadCommand::GetInfo, AlignedOutput::Info(bytes)) => OutputBuffer::Info(bytes.0),
        _ => unreachable!("output kind follows command"),
    }
}
