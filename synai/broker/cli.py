from __future__ import annotations

import logging
import os
import stat
import sys

import uvicorn

from synai.broker.app import BrokerService, create_app
from synai.broker.config import BrokerConfig


def main() -> None:
    try:
        config = BrokerConfig.from_env()
        service = BrokerService(config)
        import asyncio
        asyncio.run(service.start())
        log_role = next(
            (role for role in config.storage_roles if role.kind == "broker_logs"),
            None,
        )
        if (
            log_role is None
            or not service.quota_manager.verify_storage_role(log_role.role_id)
        ):
            raise RuntimeError("Bounded Broker log storage could not be verified")
        _configure_bounded_logging(config)
        uvicorn.run(
            create_app(service), host="127.0.0.1", port=config.listen_port,
            ssl_certfile=str(config.broker_cert_file),
            ssl_keyfile=str(config.broker_key_file),
            ssl_ca_certs=str(config.core_ca_file),
            ssl_cert_reqs=2,
            log_level="info",
            log_config=None,
            access_log=False,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"synai-broker: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _configure_bounded_logging(config: BrokerConfig) -> None:
    role = next(
        (item for item in config.storage_roles if item.kind == "broker_logs"),
        None,
    )
    if role is None:
        raise RuntimeError("Broker log storage role is not configured")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in role.path.parts[1:]:
            next_directory = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = next_directory
            parent_info = os.fstat(directory)
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid not in {0, config.service_uid}
                or (
                    parent_info.st_mode & 0o022
                    and not (
                        parent_info.st_uid == 0
                        and parent_info.st_mode & stat.S_ISVTX
                    )
                )
            ):
                raise RuntimeError("Broker log path contains an unprotected directory")
        directory_info = os.fstat(directory)
        if (
            directory_info.st_uid != config.service_uid
            or directory_info.st_gid != config.service_gid
            or directory_info.st_mode & 0o077
        ):
            raise RuntimeError("Broker log directory ownership or permissions are unsafe")
        descriptor = os.open(
            "broker.log",
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != config.service_uid
        or info.st_gid != config.service_gid
        or info.st_mode & 0o077
    ):
        os.close(descriptor)
        raise RuntimeError("Broker log file ownership or permissions are unsafe")
    stream = os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.disabled = True


if __name__ == "__main__":
    main()
