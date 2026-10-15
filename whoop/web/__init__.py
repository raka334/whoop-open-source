"""Local web dashboard (stdlib only, no build step)."""

from .server import Dashboard, build_server, serve

__all__ = ["Dashboard", "build_server", "serve"]
