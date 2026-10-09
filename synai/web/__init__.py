"""Read-only web application foundation for SynAI."""

from synai.web.app import create_app
from synai.web.config import WebConfig

__all__ = ["WebConfig", "create_app"]
