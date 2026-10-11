from __future__ import annotations

import os
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
        uvicorn.run(
            create_app(service), host="127.0.0.1", port=config.listen_port,
            ssl_certfile=str(config.broker_cert_file),
            ssl_keyfile=str(config.broker_key_file),
            ssl_ca_certs=str(config.core_ca_file),
            ssl_cert_reqs=2,
            log_level="info",
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"synai-broker: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
