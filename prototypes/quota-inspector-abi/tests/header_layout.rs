#[test]
fn installed_c_headers_match_reviewed_rust_contract() {
    let output = std::process::Command::new("cc")
        .args(["-std=c11", "-Wall", "-Wextra", "-Werror", "-fsyntax-only"])
        .arg(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/abi-layout.c"))
        .output()
        .expect("installed C compiler required for compile-only ABI verification");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
