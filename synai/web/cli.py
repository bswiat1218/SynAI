from __future__ import annotations

import uvicorn

from synai.web.app import create_app
from synai.web.config import WebConfig


def main() -> None:
    config = WebConfig.from_env()
    uvicorn.run(
        create_app(config),
        host=config.bind_host,
        port=config.port,
        access_log=False,
        proxy_headers=False,
    )
