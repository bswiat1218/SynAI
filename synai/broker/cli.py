from __future__ import annotations

import logging
import os
import stat
import sys
from collections.abc import Callable
from typing import TextIO

import uvicorn

from synai.broker.app import BrokerService, create_app
from synai.broker.config import BrokerConfig
from synai.broker.filesystem import Directory, mount_identity
from synai.broker.protocol import BrokerError


def main() -> None:
    try:
        config = BrokerConfig.from_env()
        service = BrokerService(config)
        def configure_logging() -> None:
            log_role = next(
                (role for role in config.storage_roles if role.kind == "broker_logs"),
                None,
            )
            if log_role is None or not service.quota_manager.verify_storage_role(log_role.role_id):
                raise RuntimeError("Bounded Broker log storage could not be verified")
            authority = service.quota_manager.open_storage_role(log_role.role_id)
            _configure_bounded_logging(
                config, authority=authority,
                on_failure=lambda: service._invalidate_storage_readiness("logging_authority_lost"),
            )

        uvicorn.run(
            create_app(service, startup_callback=configure_logging),
            host="127.0.0.1", port=config.listen_port,
            ssl_certfile=str(config.broker_cert_file),
            ssl_keyfile=str(config.broker_key_file),
            ssl_ca_certs=str(config.core_ca_file),
            ssl_cert_reqs=2,
            log_level="info",
            log_config=None,
            access_log=False,
            lifespan="on",
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"synai-broker: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _configure_bounded_logging(config: BrokerConfig, *, authority: Directory | None = None,
                               on_failure: Callable[[], None] | None = None) -> None:
    role = next(
        (item for item in config.storage_roles if item.kind == "broker_logs"),
        None,
    )
    if role is None:
        raise RuntimeError("Broker log storage role is not configured")
    authority = authority or Directory(role.path, owner_uid=config.service_uid,
                                      owner_gid=config.service_gid, private=True)
    try:
        descriptor = authority.open(
            "broker.log",
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except BaseException:
        authority.close()
        raise
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != config.service_uid
        or info.st_gid != config.service_gid
        or info.st_mode & 0o077
    ):
        os.close(descriptor)
        authority.close()
        raise RuntimeError("Broker log file ownership or permissions are unsafe")
    stream = os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)
    handler = _PinnedLogHandler(stream, authority, mount_identity(descriptor), on_failure)
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


class _PinnedLogHandler(logging.StreamHandler):
    def __init__(self, stream: TextIO, authority: Directory,
                 identity: tuple[int, int, int], on_failure: Callable[[], None] | None = None) -> None:
        super().__init__(stream)
        self.authority = authority
        self.identity = identity
        self.on_failure = on_failure

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emit_verified(record)
        except (BrokerError, OSError, RuntimeError):
            if self.on_failure is not None:
                self.on_failure()
            raise

    def _emit_verified(self, record: logging.LogRecord) -> None:
        self.authority.revalidate()
        descriptor = self.authority.open("broker.log", os.O_PATH)
        try:
            if mount_identity(descriptor) != self.identity:
                raise RuntimeError("Broker log file was substituted; logging refused")
        finally:
            os.close(descriptor)
        if self.stream is None:
            raise RuntimeError("Broker log stream is unavailable")
        self.stream.write(self.format(record) + self.terminator)
        self.flush()
        self.authority.revalidate()

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
        self.authority.close()
        super().close()


if __name__ == "__main__":
    main()
