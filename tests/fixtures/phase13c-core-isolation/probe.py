import json
import os
import sys
from pathlib import Path


def main() -> None:
    checks = {
        "uid": os.getuid(),
        "writable_data_volume": False,
        "writable_snapshot_volume": False,
        "read_only_root": False,
        "docker_socket_absent": not Path("/var/run/docker.sock").exists(),
        "podman_socket_absent": not Path("/run/podman/podman.sock").exists(),
        "host_workspace_unmounted": not Path(sys.argv[1]).exists(),
    }
    data_file = Path("/data/probe-marker")
    data_file.write_text("disposable", encoding="utf-8")
    checks["writable_data_volume"] = data_file.read_text(encoding="utf-8") == "disposable"
    try:
        snapshot_marker = Path("/snapshots/probe-marker")
        snapshot_marker.write_text("disposable", encoding="utf-8")
        checks["writable_snapshot_volume"] = snapshot_marker.read_text(encoding="utf-8") == "disposable"
    except OSError:
        checks["writable_snapshot_volume"] = False
    try:
        Path("/etc/synai-phase13c-probe").write_text("must fail", encoding="utf-8")
    except OSError:
        checks["read_only_root"] = True
    print(json.dumps(checks, sort_keys=True))
    if not all(value is True for key, value in checks.items() if key != "uid"):
        raise SystemExit(1)
    if checks["uid"] == 0:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
