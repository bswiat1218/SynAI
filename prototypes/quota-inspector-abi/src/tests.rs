use std::fs::File;
use std::os::fd::OwnedFd;
use std::os::fd::{AsFd, AsRawFd};
use std::sync::{Arc, Weak};

use crate::transport::{KernelDispatcher, invoke_quotactl_fd_with};
use crate::*;

fn quota_bytes() -> [u8; 72] {
    let mut bytes = QuotaRecord::initialized_buffer();
    for (offset, value) in [
        (0, 8_u64),
        (8, 4),
        (16, 1234),
        (24, 100),
        (32, 50),
        (40, 7),
        (48, 1700000000),
        (56, 1700000001),
    ] {
        bytes[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
    }
    bytes[64..68].copy_from_slice(&0x3f_u32.to_le_bytes());
    bytes
}

fn info_bytes() -> [u8; 24] {
    let mut bytes = InfoRecord::initialized_buffer();
    bytes[0..8].copy_from_slice(&604800_u64.to_le_bytes());
    bytes[8..16].copy_from_slice(&3600_u64.to_le_bytes());
    bytes[16..20].copy_from_slice(&0x10001_u32.to_le_bytes());
    bytes[20..24].copy_from_slice(&7_u32.to_le_bytes());
    bytes
}

fn set_word(bytes: &mut [u8], offset: usize, value: u64) {
    bytes[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Failure {
    Cancelled,
    Timeout,
    Errno(ErrnoClass),
    Decode(AbiError),
}

enum Reply {
    Pending,
    Complete(Result<Vec<u8>, ErrnoClass>),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Candidate {
    Quota(QuotaCandidate),
    Info(InfoCandidate),
}

// Contract model only: the native library's permanent tests exercise its actual state machine.
struct FakeTransport {
    worker_lease: Option<Arc<OwnedFd>>,
}

struct FakeOperation {
    command: ReadCommand,
    owner_lease: Option<Arc<OwnedFd>>,
    terminal: Option<Failure>,
    complete: bool,
    now: u64,
    deadline: u64,
}

impl FakeOperation {
    fn new(command: ReadCommand) -> (Self, FakeTransport, Weak<OwnedFd>) {
        // An inert FD for ownership tests, not a filesystem-qualified quota descriptor.
        let fd: OwnedFd = File::open(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml"))
            .unwrap()
            .into();
        let lease = Arc::new(fd);
        let weak = Arc::downgrade(&lease);
        let transport = FakeTransport {
            worker_lease: Some(Arc::clone(&lease)),
        };
        (
            Self {
                command,
                owner_lease: Some(lease),
                terminal: None,
                complete: false,
                now: 0,
                deadline: 10,
            },
            transport,
            weak,
        )
    }

    fn check_deadline(&mut self) {
        if self.now >= self.deadline {
            self.terminal.get_or_insert(Failure::Timeout);
        }
    }

    fn cancel(&mut self) {
        if !self.complete {
            self.check_deadline();
            self.terminal.get_or_insert(Failure::Cancelled);
        }
    }

    fn poll(
        &mut self,
        transport: &mut FakeTransport,
        reply: Reply,
    ) -> Result<Option<Candidate>, Failure> {
        self.check_deadline();
        if self.complete {
            return Err(self.terminal.expect("only failed operations are repolled"));
        }
        match reply {
            Reply::Pending => self.terminal.map_or(Ok(None), Err),
            Reply::Complete(output) => {
                self.complete = true;
                transport.worker_lease = None;
                self.owner_lease = None;
                if let Some(failure) = self.terminal {
                    return Err(failure);
                }
                let outcome = output.map_err(Failure::Errno).and_then(|bytes| {
                    match self.command {
                        ReadCommand::GetQuota(_) => QuotaRecord::from_bytes(&bytes)
                            .and_then(QuotaRecord::decode)
                            .map(Candidate::Quota),
                        ReadCommand::GetInfo => InfoRecord::from_bytes(&bytes)
                            .and_then(InfoRecord::decode)
                            .map(Candidate::Info),
                    }
                    .map_err(Failure::Decode)
                });
                match outcome {
                    Ok(candidate) => Ok(Some(candidate)),
                    Err(failure) => {
                        self.terminal.get_or_insert(failure);
                        Err(failure)
                    }
                }
            }
        }
    }
}

fn command() -> ReadCommand {
    ReadCommand::GetQuota(ProjectId::new(4242).unwrap())
}

#[test]
fn exact_command_encoding_and_argument_widths() {
    assert_eq!(command().encoded(), Ok(0x80000702));
    assert_eq!(ReadCommand::GetInfo.encoded(), Ok(0x80000502));
    assert_eq!(command().id_argument(), 4242);
    assert_eq!(ReadCommand::GetInfo.id_argument(), 0);
    assert_eq!(command().buffer_size(), QuotaRecord::SIZE);
    assert_eq!(ReadCommand::GetInfo.buffer_size(), InfoRecord::SIZE);
    assert_eq!(SYSCALL_NUMBER, 443);
    assert_eq!(
        ProjectId::new(u64::from(u32::MAX - 1)).unwrap().value(),
        u32::MAX - 1
    );
}

#[test]
fn invalid_project_ids_are_unrepresentable() {
    for id in [0, u64::from(u32::MAX), u64::from(u32::MAX) + 1, u64::MAX] {
        assert_eq!(ProjectId::new(id), Err(AbiError::InvalidProjectId));
    }
}

#[test]
fn successful_fake_getquota_preserves_units_and_expiries() {
    let (mut operation, mut transport, weak) = FakeOperation::new(command());
    let candidate = operation
        .poll(&mut transport, Reply::Complete(Ok(quota_bytes().to_vec())))
        .unwrap()
        .unwrap();
    let Candidate::Quota(candidate) = candidate else {
        panic!("wrong candidate")
    };
    assert_eq!(candidate.positive_hard_caps(), Ok((8192, 100)));
    assert_eq!(candidate.soft_bytes, ByteLimit::Bytes(4096));
    assert_eq!(candidate.soft_inodes, InodeLimit::Inodes(50));
    assert_eq!((candidate.used_bytes, candidate.used_inodes), (1234, 7));
    assert_eq!(candidate.byte_expiry, Some(1700000000));
    assert_eq!(candidate.inode_expiry, Some(1700000001));
    assert!(weak.upgrade().is_none());
}

#[test]
fn successful_fake_getinfo_is_not_enforcement_evidence() {
    let (mut operation, mut transport, _) = FakeOperation::new(ReadCommand::GetInfo);
    assert_eq!(
        operation.poll(&mut transport, Reply::Complete(Ok(info_bytes().to_vec()))),
        Ok(Some(Candidate::Info(InfoCandidate {
            byte_grace_seconds: 604800,
            inode_grace_seconds: 3600,
            root_squash: true,
            system_file: true
        })))
    );
}

#[test]
fn errno_classification_preserves_unknown_and_ambiguous_errors() {
    let cases = [
        (38, ErrnoClass::UnsupportedInterface),
        (95, ErrnoClass::UnsupportedOperation),
        (1, ErrnoClass::PermissionDenied),
        (13, ErrnoClass::PermissionDenied),
        (9, ErrnoClass::InvalidDescriptor),
        (22, ErrnoClass::InvalidArgument),
        (2, ErrnoClass::MissingRecord),
        (3, ErrnoClass::MissingRecord),
        (61, ErrnoClass::MissingRecord),
        (4, ErrnoClass::Interrupted),
        (5, ErrnoClass::IoFailure),
        (14, ErrnoClass::BadBuffer),
        (125, ErrnoClass::Cancelled),
        (110, ErrnoClass::TimedOut),
    ];
    for (errno, expected) in cases {
        assert_eq!(classify_errno(errno), expected);
        let (mut operation, mut transport, weak) = FakeOperation::new(command());
        assert_eq!(
            operation.poll(&mut transport, Reply::Complete(Err(classify_errno(errno)))),
            Err(Failure::Errno(expected))
        );
        assert!(weak.upgrade().is_none());
    }
    for errno in [0, -38, 123456] {
        assert_eq!(classify_errno(errno), ErrnoClass::Unknown(errno));
    }
}

#[test]
fn malformed_buffers_never_decode() {
    for size in 0..=100 {
        if size != 72 {
            assert_eq!(
                QuotaRecord::from_bytes(&vec![0; size]),
                Err(AbiError::BufferSize)
            );
        }
        if size != 24 {
            assert_eq!(
                InfoRecord::from_bytes(&vec![0; size]),
                Err(AbiError::BufferSize)
            );
        }
    }
    let (mut operation, mut transport, _) = FakeOperation::new(command());
    assert_eq!(
        operation.poll(&mut transport, Reply::Complete(Ok(vec![0; 71]))),
        Err(Failure::Decode(AbiError::BufferSize))
    );
}

#[test]
fn initialized_buffers_and_default_records_have_no_valid_evidence() {
    assert_eq!(QuotaRecord::initialized_buffer(), [0; 72]);
    assert_eq!(InfoRecord::initialized_buffer(), [0; 24]);
    assert_eq!(
        QuotaRecord::default().decode(),
        Err(AbiError::MissingValidity)
    );
    assert_eq!(
        InfoRecord::default().decode(),
        Err(AbiError::MissingValidity)
    );
}

#[test]
fn all_required_quota_validity_bits_are_enforced() {
    for mask in 0_u32..64 {
        let mut bytes = quota_bytes();
        bytes[64..68].copy_from_slice(&mask.to_le_bytes());
        let result = QuotaRecord::from_bytes(&bytes).unwrap().decode();
        if mask & 15 != 15 {
            assert_eq!(result, Err(AbiError::MissingValidity));
        } else {
            let candidate = result.unwrap();
            assert_eq!(candidate.byte_expiry.is_some(), mask & 16 != 0);
            assert_eq!(candidate.inode_expiry.is_some(), mask & 32 != 0);
        }
    }
}

#[test]
fn all_info_validity_bits_are_enforced() {
    for mask in 0_u32..7 {
        let mut bytes = info_bytes();
        bytes[20..24].copy_from_slice(&mask.to_le_bytes());
        assert_eq!(
            InfoRecord::from_bytes(&bytes).unwrap().decode(),
            Err(AbiError::MissingValidity)
        );
    }
}

#[test]
fn unknown_validity_and_info_flags_fail_closed() {
    for bit in 6..32 {
        let mut bytes = quota_bytes();
        bytes[64..68].copy_from_slice(&(0x3f_u32 | (1 << bit)).to_le_bytes());
        assert_eq!(
            QuotaRecord::from_bytes(&bytes).unwrap().decode(),
            Err(AbiError::UnknownValidity)
        );
    }
    for bit in 3..32 {
        let mut bytes = info_bytes();
        bytes[20..24].copy_from_slice(&(7_u32 | (1 << bit)).to_le_bytes());
        assert_eq!(
            InfoRecord::from_bytes(&bytes).unwrap().decode(),
            Err(AbiError::UnknownValidity)
        );
    }
    for bit in 0..32 {
        if bit == 0 || bit == 16 {
            continue;
        }
        let mut bytes = info_bytes();
        bytes[16..20].copy_from_slice(&(1_u32 << bit).to_le_bytes());
        assert_eq!(
            InfoRecord::from_bytes(&bytes).unwrap().decode(),
            Err(AbiError::UnknownFlags)
        );
    }
}

#[test]
fn zero_limits_are_explicitly_unlimited_not_positive_caps() {
    let mut bytes = quota_bytes();
    for offset in [0, 8, 24, 32] {
        set_word(&mut bytes, offset, 0);
    }
    let candidate = QuotaRecord::from_bytes(&bytes).unwrap().decode().unwrap();
    assert_eq!(candidate.hard_bytes, ByteLimit::Unlimited);
    assert_eq!(candidate.soft_bytes, ByteLimit::Unlimited);
    assert_eq!(candidate.hard_inodes, InodeLimit::Unlimited);
    assert_eq!(candidate.soft_inodes, InodeLimit::Unlimited);
    assert_eq!(
        candidate.positive_hard_caps(),
        Err(AbiError::UnlimitedHardLimit)
    );
    let invented = QuotaCandidate {
        hard_bytes: ByteLimit::Bytes(0),
        hard_inodes: InodeLimit::Inodes(0),
        ..candidate
    };
    assert_eq!(
        invented.positive_hard_caps(),
        Err(AbiError::UnlimitedHardLimit)
    );
}

#[test]
fn counter_overflow_and_maximum_representable_block_limit() {
    let maximum = u64::MAX / 1024;
    let mut bytes = quota_bytes();
    set_word(&mut bytes, 0, maximum);
    assert_eq!(
        QuotaRecord::from_bytes(&bytes)
            .unwrap()
            .decode()
            .unwrap()
            .hard_bytes,
        ByteLimit::Bytes(maximum * 1024)
    );
    for value in [maximum + 1, u64::MAX] {
        set_word(&mut bytes, 0, value);
        assert_eq!(
            QuotaRecord::from_bytes(&bytes).unwrap().decode(),
            Err(AbiError::Overflow)
        );
    }
    set_word(&mut bytes, 0, 0);
    set_word(&mut bytes, 8, u64::MAX);
    assert_eq!(
        QuotaRecord::from_bytes(&bytes).unwrap().decode(),
        Err(AbiError::Overflow)
    );
}

#[test]
fn current_space_and_inode_counters_are_not_scaled_or_truncated() {
    let mut bytes = quota_bytes();
    set_word(&mut bytes, 16, u64::MAX);
    set_word(&mut bytes, 24, u64::MAX);
    set_word(&mut bytes, 40, u64::MAX);
    let candidate = QuotaRecord::from_bytes(&bytes).unwrap().decode().unwrap();
    assert_eq!(candidate.used_bytes, u64::MAX);
    assert_eq!(candidate.used_inodes, u64::MAX);
    assert_eq!(candidate.hard_inodes, InodeLimit::Inodes(u64::MAX));
}

#[test]
fn quota_units_are_1024_not_filesystem_blocks_or_current_space_units() {
    let mut bytes = quota_bytes();
    set_word(&mut bytes, 0, 1);
    set_word(&mut bytes, 8, 1);
    set_word(&mut bytes, 16, 1);
    let candidate = QuotaRecord::from_bytes(&bytes).unwrap().decode().unwrap();
    assert_eq!(candidate.hard_bytes, ByteLimit::Bytes(1024));
    assert_ne!(candidate.hard_bytes, ByteLimit::Bytes(4096));
    assert_eq!(candidate.used_bytes, 1);
}

#[test]
fn contradictory_soft_and_hard_limits_are_rejected() {
    for (offset, value) in [(8, 9), (32, 101)] {
        let mut bytes = quota_bytes();
        set_word(&mut bytes, offset, value);
        assert_eq!(
            QuotaRecord::from_bytes(&bytes).unwrap().decode(),
            Err(AbiError::ContradictoryLimits)
        );
    }
    let mut bytes = quota_bytes();
    bytes[16..24].copy_from_slice(&u64::MAX.to_le_bytes());
    // Usage exceeding a limit is preserved as untrusted data, not enforcement proof.
    assert!(QuotaRecord::from_bytes(&bytes).unwrap().decode().is_ok());
}

#[test]
fn kernel_tail_padding_is_ignored_and_never_becomes_evidence() {
    let bytes = quota_bytes();
    let expected = QuotaRecord::from_bytes(&bytes).unwrap();
    let mut nonzero_tail = bytes;
    nonzero_tail[68..72].fill(255);
    assert_eq!(QuotaRecord::from_bytes(&nonzero_tail).unwrap(), expected);
}

#[test]
fn transient_pending_then_success_before_deadline() {
    let (mut operation, mut transport, weak) = FakeOperation::new(command());
    for now in 0..9 {
        operation.now = now;
        assert_eq!(operation.poll(&mut transport, Reply::Pending), Ok(None));
        assert!(!operation.complete);
        assert_eq!(weak.strong_count(), 2);
    }
    assert!(
        operation
            .poll(&mut transport, Reply::Complete(Ok(quota_bytes().to_vec())))
            .is_ok()
    );
    assert!(weak.upgrade().is_none());
}

#[test]
fn cancellation_and_timeout_retain_leases_and_refuse_every_late_outcome() {
    for reason in [Failure::Cancelled, Failure::Timeout] {
        for late in [
            Ok(quota_bytes().to_vec()),
            Ok(vec![0; 72]),
            Err(ErrnoClass::IoFailure),
        ] {
            let (mut operation, mut transport, weak) = FakeOperation::new(command());
            if reason == Failure::Cancelled {
                operation.cancel();
            } else {
                operation.now = 10;
            }
            for _ in 0..20 {
                assert_eq!(operation.poll(&mut transport, Reply::Pending), Err(reason));
                assert!(!operation.complete);
                assert_eq!(weak.strong_count(), 2);
                operation.cancel();
            }
            assert_eq!(
                operation.poll(&mut transport, Reply::Complete(late)),
                Err(reason)
            );
            assert!(operation.complete);
            assert!(weak.upgrade().is_none());
            for _ in 0..3 {
                assert_eq!(operation.poll(&mut transport, Reply::Pending), Err(reason));
            }
        }
    }
}

#[test]
fn first_terminal_reason_wins_near_deadline() {
    let (mut operation, _, _) = FakeOperation::new(command());
    operation.now = 9;
    operation.cancel();
    operation.now = 10;
    operation.check_deadline();
    assert_eq!(operation.terminal, Some(Failure::Cancelled));
    let (mut operation, _, _) = FakeOperation::new(command());
    operation.now = 10;
    operation.cancel();
    assert_eq!(operation.terminal, Some(Failure::Timeout));
}

#[test]
fn dropped_cancelled_owner_cannot_orphan_physical_work_or_admit_overlap() {
    let (mut operation, mut transport, active) = FakeOperation::new(command());
    operation.cancel();
    operation.cancel();
    drop(operation);
    assert_eq!(active.strong_count(), 1);
    assert!(
        active.upgrade().is_some(),
        "busy while worker retains active lease"
    );
    // Simulated externally confirmed physical completion, not request cancellation.
    transport.worker_lease = None;
    assert!(active.upgrade().is_none());
    let (mut fresh, mut transport, _) = FakeOperation::new(command());
    assert!(
        fresh
            .poll(&mut transport, Reply::Complete(Ok(quota_bytes().to_vec())))
            .is_ok()
    );
}

#[test]
fn reported_kernel_and_libc_status_are_distinct_and_checked() {
    assert!(ReportedStatus::from_kernel(0).is_ok());
    assert!(ReportedStatus::from_libc(0, 38).is_ok());
    for errno in [1, 4, 9, 22, 38, 95, 4095] {
        let expected = Err(AcquisitionError::Errno(classify_errno(errno)));
        assert_eq!(ReportedStatus::from_kernel(-i64::from(errno)), expected);
        assert_eq!(ReportedStatus::from_libc(-1, errno), expected);
    }
    for value in [1, i64::MAX, -4096, i64::MIN] {
        assert_eq!(
            ReportedStatus::from_kernel(value),
            Err(AcquisitionError::InvalidReturn)
        );
    }
    for errno in [0, -1, 4096, i32::MAX] {
        assert_eq!(
            ReportedStatus::from_libc(-1, errno),
            Err(AcquisitionError::InvalidErrno)
        );
    }
    for value in [-38, -2, 1, i64::MIN] {
        assert_eq!(
            ReportedStatus::from_libc(value, 38),
            Err(AcquisitionError::InvalidReturn)
        );
    }
}

#[test]
fn acquisition_kind_and_reported_failure_gate_untrusted_decoding() {
    let file = File::open(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml")).unwrap();
    for command in [command(), ReadCommand::GetInfo] {
        let acquisition = Acquisition::new(file.as_fd(), command);
        assert_eq!(acquisition.command(), command);
        assert_eq!(acquisition.descriptor().as_raw_fd(), file.as_raw_fd());
        let output = match command {
            ReadCommand::GetQuota(_) => OutputBuffer::Quota(quota_bytes()),
            ReadCommand::GetInfo => OutputBuffer::Info(info_bytes()),
        };
        assert!(
            acquisition
                .decode_reported_output(ReportedStatus::from_kernel(0), output.clone())
                .is_ok()
        );
        for errno in [4, 22, 38, 95] {
            assert_eq!(
                acquisition.decode_reported_output(
                    ReportedStatus::from_kernel(-i64::from(errno)),
                    output.clone()
                ),
                Err(AcquisitionError::Errno(classify_errno(errno)))
            );
        }
        assert_eq!(
            acquisition.decode_reported_output(
                ReportedStatus::from_kernel(0),
                acquisition.initialized_output()
            ),
            Err(AcquisitionError::Decode(AbiError::MissingValidity))
        );
        let wrong = match command {
            ReadCommand::GetQuota(_) => OutputBuffer::Info(info_bytes()),
            ReadCommand::GetInfo => OutputBuffer::Quota(quota_bytes()),
        };
        assert_eq!(
            acquisition.decode_reported_output(ReportedStatus::from_kernel(0), wrong),
            Err(AcquisitionError::OutputKind)
        );
    }
}

struct DispatcherReply {
    status: Result<ReportedStatus, AcquisitionError>,
    output: Vec<u8>,
}

struct RecordingDispatcher {
    replies: Vec<DispatcherReply>,
    calls: Vec<(u32, u32, u32, usize)>,
}

impl RecordingDispatcher {
    fn new(replies: Vec<DispatcherReply>) -> Self {
        Self {
            replies,
            calls: Vec::new(),
        }
    }
}

impl KernelDispatcher for RecordingDispatcher {
    fn invoke(
        &mut self,
        fd: u32,
        command: u32,
        id: u32,
        output: &mut [u8],
    ) -> Result<ReportedStatus, AcquisitionError> {
        self.calls.push((fd, command, id, output.len()));
        let reply = self.replies.remove(0);
        if output.len() != reply.output.len() {
            return Err(AcquisitionError::OutputKind);
        }
        output.copy_from_slice(&reply.output);
        reply.status
    }
}

#[test]
fn invoke_transport_binds_exact_fd_command_id_and_output_size() {
    let file = File::open(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml")).unwrap();
    let project = ProjectId::new(4242).unwrap();
    let quota = Acquisition::new(file.as_fd(), ReadCommand::GetQuota(project));
    let mut dispatcher = RecordingDispatcher::new(vec![DispatcherReply {
        status: ReportedStatus::from_libc(0, 0),
        output: quota_bytes().to_vec(),
    }]);
    let quota_candidate = invoke_quotactl_fd_with(&quota, &mut dispatcher).unwrap();
    assert!(matches!(quota_candidate, DecodedCandidate::Quota(_)));
    let expected_fd = u32::try_from(file.as_raw_fd()).unwrap();
    assert_eq!(
        dispatcher.calls,
        vec![(expected_fd, 0x80000702, project.value(), 72)]
    );

    let info = Acquisition::new(file.as_fd(), ReadCommand::GetInfo);
    let mut dispatcher = RecordingDispatcher::new(vec![DispatcherReply {
        status: ReportedStatus::from_libc(0, 0),
        output: info_bytes().to_vec(),
    }]);
    let info_candidate = invoke_quotactl_fd_with(&info, &mut dispatcher).unwrap();
    assert!(matches!(info_candidate, DecodedCandidate::Info(_)));
    assert_eq!(dispatcher.calls, vec![(expected_fd, 0x80000502, 0, 24)]);
}

#[test]
fn invoke_transport_fails_closed_on_status_output_and_decode_errors() {
    let file = File::open(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml")).unwrap();
    let acquisition = Acquisition::new(
        file.as_fd(),
        ReadCommand::GetQuota(ProjectId::new(4242).unwrap()),
    );

    let mut dispatcher = RecordingDispatcher::new(vec![DispatcherReply {
        status: ReportedStatus::from_libc(-1, 38),
        output: quota_bytes().to_vec(),
    }]);
    assert_eq!(
        invoke_quotactl_fd_with(&acquisition, &mut dispatcher),
        Err(AcquisitionError::Errno(ErrnoClass::UnsupportedInterface))
    );

    let mut dispatcher = RecordingDispatcher::new(vec![DispatcherReply {
        status: ReportedStatus::from_libc(0, 0),
        output: vec![0; 71],
    }]);
    assert_eq!(
        invoke_quotactl_fd_with(&acquisition, &mut dispatcher),
        Err(AcquisitionError::OutputKind)
    );

    let mut dispatcher = RecordingDispatcher::new(vec![DispatcherReply {
        status: ReportedStatus::from_libc(0, 0),
        output: vec![0; 72],
    }]);
    assert_eq!(
        invoke_quotactl_fd_with(&acquisition, &mut dispatcher),
        Err(AcquisitionError::Decode(AbiError::MissingValidity))
    );
}

#[test]
#[should_panic(expected = "real quotactl_fd transport is disabled in tests")]
fn real_transport_entrypoint_never_dispatches_from_unit_tests() {
    let file = File::open(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml")).unwrap();
    let acquisition = Acquisition::new(
        file.as_fd(),
        ReadCommand::GetQuota(ProjectId::new(4242).unwrap()),
    );
    let _ = invoke_quotactl_fd(&acquisition);
}
