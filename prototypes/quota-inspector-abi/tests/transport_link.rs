use quota_inspector_abi::{Acquisition, AcquisitionError, DecodedCandidate, invoke_quotactl_fd};

#[test]
fn callable_transport_links_without_being_invoked() {
    let entry: fn(&Acquisition<'_>) -> Result<DecodedCandidate, AcquisitionError> =
        invoke_quotactl_fd;
    assert_ne!(std::hint::black_box(entry) as usize, 0);
}
