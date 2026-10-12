pub const SUPPORTED_TARGET: &str = "x86_64-unknown-linux-gnu (LP64, little-endian)";
/// Header identity only; no code dispatches this number.
pub const SYSCALL_NUMBER: u32 = 443;

const _: () = {
    assert!(size_of::<usize>() == 8);
    assert!(size_of::<std::ffi::c_uint>() == 4);
    assert!(size_of::<std::ffi::c_long>() == 8);
};
