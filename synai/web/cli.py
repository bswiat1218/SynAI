from __future__ import annotations

import asyncio
import ssl

import uvicorn

from synai.web.app import create_app
from synai.web.broker_control import BrokerControlConfig, create_broker_control_app
from synai.web.config import WebConfig


def main() -> None:
    config = WebConfig.from_env()
    web_app = create_app(config)
    control = BrokerControlConfig.from_env()
    if control is None:
        uvicorn.run(
            web_app,
            host=config.bind_host,
            port=config.port,
            access_log=False,
            proxy_headers=False,
        )
        return
    if control.port == config.port:
        raise ValueError("Core Broker control listener must use a separate loopback port")
    control_app = create_broker_control_app(web_app, control)

    async def serve() -> None:
        web_server = uvicorn.Server(uvicorn.Config(
            web_app, host=config.bind_host, port=config.port,
            access_log=False, proxy_headers=False,
        ))
        control_server = uvicorn.Server(uvicorn.Config(
            control_app,
            host="127.0.0.1",
            port=control.port,
            access_log=False,
            proxy_headers=False,
            ssl_certfile=str(control.core_cert_file),
            ssl_keyfile=str(control.core_key_file),
            ssl_ca_certs=str(control.broker_ca_file),
            ssl_cert_reqs=ssl.CERT_REQUIRED,
        ))
        await asyncio.gather(web_server.serve(), control_server.serve())

    asyncio.run(serve())
