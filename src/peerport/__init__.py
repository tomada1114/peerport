"""Public package interface for peerport."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .config import Config, load_config
from .core import add
from .db import Database, backup_db, open_db, reset_fresh, rotate_backups
from .server.app import create_app

try:
    __version__ = version("peerport")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "Config",
    "Database",
    "__version__",
    "add",
    "backup_db",
    "create_app",
    "load_config",
    "open_db",
    "reset_fresh",
    "rotate_backups",
]
