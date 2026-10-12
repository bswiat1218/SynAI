#![forbid(unsafe_code)]

use std::path::PathBuf;
use std::time::Duration;

use quota_inspector::{Backend, Inventory, run_harness};

fn main() {
    if let Err(error) = run() {
        eprintln!("fixture-harness: {error}");
        std::process::exit(2);
    }
}

fn run() -> Result<(), String> {
    let mut parent = None;
    let mut inventory_path = None;
    let mut allowed_uid = None;
    let mut allowed_gid = None;
    let mut lifetime_ms = None;
    let mut backend = Backend::Fixture;
    let mut saw_backend = false;
    let mut args = std::env::args_os().skip(1);
    while let Some(argument) = args.next() {
        let flag = argument.to_str().ok_or("arguments must be UTF-8")?;
        let value = args
            .next()
            .ok_or_else(|| format!("missing value for {flag}"))?;
        match flag {
            "--parent" if parent.is_none() => parent = Some(PathBuf::from(value)),
            "--inventory" if inventory_path.is_none() => {
                inventory_path = Some(PathBuf::from(value))
            }
            "--allowed-uid" if allowed_uid.is_none() => {
                allowed_uid = Some(parse_u32(&value, flag)?)
            }
            "--allowed-gid" if allowed_gid.is_none() => {
                allowed_gid = Some(parse_u32(&value, flag)?)
            }
            "--lifetime-ms" if lifetime_ms.is_none() => {
                let text = value.to_str().ok_or("lifetime must be UTF-8")?;
                lifetime_ms = Some(text.parse::<u64>().map_err(|_| "invalid lifetime")?);
            }
            "--backend" if !saw_backend => {
                backend = match value.to_str().ok_or("backend must be UTF-8")? {
                    "fixture" => Backend::Fixture,
                    "native-unavailable" => Backend::NativeUnavailable,
                    _ => return Err("backend must be fixture or native-unavailable".to_owned()),
                };
                saw_backend = true;
            }
            _ => return Err(format!("unknown or duplicate argument: {flag}")),
        }
    }
    let parent = parent.ok_or("--parent is required")?;
    let inventory_path = inventory_path.ok_or("--inventory is required")?;
    let allowed_uid = allowed_uid.ok_or("--allowed-uid is required")?;
    let allowed_gid = allowed_gid.ok_or("--allowed-gid is required")?;
    let lifetime_ms = lifetime_ms.ok_or("--lifetime-ms is required")?;
    if !parent.is_absolute() {
        return Err("--parent must be absolute".to_owned());
    }
    if lifetime_ms == 0 || lifetime_ms > 30_000 {
        return Err("--lifetime-ms must be in 1..=30000".to_owned());
    }
    let inventory = Inventory::load(&inventory_path)?;
    run_harness(
        &parent,
        &inventory,
        allowed_uid,
        allowed_gid,
        Duration::from_millis(lifetime_ms),
        backend,
    )
}

fn parse_u32(value: &std::ffi::OsStr, flag: &str) -> Result<u32, String> {
    value
        .to_str()
        .ok_or_else(|| format!("{flag} must be UTF-8"))?
        .parse::<u32>()
        .map_err(|_| format!("invalid value for {flag}"))
}
