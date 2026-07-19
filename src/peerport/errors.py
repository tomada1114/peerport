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


class BudgetExceededError(PeerPortError):
    """Raised when the daily hard spending cap refuses further LLM calls."""


class LLMCallError(PeerPortError):
    """Raised when an LLM call fails after exhausting its retry budget."""


class InvalidMemoryKindError(PeerPortError):
    """Raised when a memory write uses a kind outside the allowed set."""


class NoteNotFoundError(PeerPortError):
    """Raised when `append`/`read` targets a nonexistent note id."""


class NoteOperationRejectedError(PeerPortError):
    """Raised when a function call names an operation outside the 5 exposed to Mate."""
