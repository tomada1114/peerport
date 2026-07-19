"""Package-level exception hierarchy for peerport."""

from __future__ import annotations


class PeerPortError(Exception):
    """Base class for all peerport-specific errors."""


class ConfigError(PeerPortError):
    """Raised when `config.toml` contains an invalid or malformed value."""


class DatabaseShutdownError(PeerPortError):
    """Raised when consecutive DB write failures require a safe shutdown."""


class PersonaValidationError(PeerPortError):
    """Raised when a persona Markdown file fails validation."""


class MapDataError(PeerPortError):
    """Raised when `data/map/port.json` is missing or malformed."""
