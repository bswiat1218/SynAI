from __future__ import annotations

import os
import socket
import stat
import sys
from pathlib import Path

import uvicorn

from synai.broker.app import BrokerService, create_app
from synai.broker.config import BrokerConfig


def main() -> None:
    try:
        config = BrokerConfig.from_env()
        if config.socket_path is None:
            raise ValueError("SYNAI_BROKER_SOCKET must name the private Broker Unix socket")
        parent = config.socket_path.parent
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_gid != config.socket_gid
            or info.st_mode & 0o027 or info.st_mode & 0o2000 == 0
        ):
            raise ValueError("Broker socket directory must be Broker-owned, setgid, and restricted to the provisioned Core group")
        if config.socket_path.exists() or config.socket_path.is_symlink():
            existing = config.socket_path.lstat()
            if (
                not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid()
            ):
                raise ValueError("Refusing to replace an unowned or non-socket Broker path")
            config.socket_path.unlink()
        service = BrokerService(config)
        import asyncio
        asyncio.run(service.start())
        os.umask(0o117)
        uvicorn.run(create_app(service), uds=str(config.socket_path), log_level="info")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"synai-broker: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
