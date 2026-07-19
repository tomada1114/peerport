"""Load and validate `config.toml`, falling back to documented defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from peerport.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path

VALID_LOCALES = ("en", "ja")

DEFAULT_LOCALE = "en"
DEFAULT_MODEL_BACKGROUND = "gpt-5-nano"
DEFAULT_MODEL_MATE = "gpt-5-mini"
DEFAULT_BUDGET_SOFT_CAP_USD = 0.50
DEFAULT_BUDGET_HARD_CAP_USD = 2.00
DEFAULT_TICK_MS = 500
DEFAULT_DAY_LENGTH_REAL_MINUTES = 120
DEFAULT_SERVER_PORT = 8712


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    """Model names for background simulation vs. Mate chat."""

    background: str = DEFAULT_MODEL_BACKGROUND
    mate: str = DEFAULT_MODEL_MATE


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    """Daily spend caps in USD."""

    soft_cap_usd: float = DEFAULT_BUDGET_SOFT_CAP_USD
    hard_cap_usd: float = DEFAULT_BUDGET_HARD_CAP_USD


@dataclass(frozen=True, slots=True)
class WorldConfig:
    """World clock and tick cadence settings."""

    tick_ms: int = DEFAULT_TICK_MS
    day_length_real_minutes: int = DEFAULT_DAY_LENGTH_REAL_MINUTES


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """HTTP/WebSocket server settings."""

    port: int = DEFAULT_SERVER_PORT


@dataclass(frozen=True, slots=True)
class LogbookConfig:
    """Logbook generation toggles (#22)."""

    weekly_summary: bool = True


@dataclass(frozen=True, slots=True)
class Config:
    """Fully resolved application configuration."""

    locale: str = DEFAULT_LOCALE
    models: ModelsConfig = field(default_factory=ModelsConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    world: WorldConfig = field(default_factory=WorldConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    logbook: LogbookConfig = field(default_factory=LogbookConfig)


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Return a TOML sub-table, defaulting to empty when absent."""
    value = data.get(key, {})
    if not isinstance(value, dict):
        msg = f"{key}: expected a table, got {value!r}"
        raise ConfigError(msg)
    return value


def _require_number(section: str, key: str, value: object) -> float:
    """Validate that *value* is numeric, raising `ConfigError` naming the field."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{section}.{key}: expected a number, got {value!r}"
        raise ConfigError(msg)
    return float(value)


def _require_int(section: str, key: str, value: object) -> int:
    """Validate that *value* is an integer, raising `ConfigError` naming the field."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{section}.{key}: expected an integer, got {value!r}"
        raise ConfigError(msg)
    return value


def _require_str(section: str, key: str, value: object) -> str:
    """Validate that *value* is a string, raising `ConfigError` naming the field."""
    if not isinstance(value, str):
        msg = f"{section}.{key}: expected a string, got {value!r}"
        raise ConfigError(msg)
    return value


def load_config(path: Path) -> Config:
    """Load configuration from *path*, falling back to defaults for absent fields.

    Args:
        path: Location of `config.toml`. Missing files are treated as empty.

    Returns:
        A fully resolved `Config` with every field populated.

    Raises:
        ConfigError: If the file is malformed TOML, or a present field has an
            invalid type or value (e.g. `locale = "fr"`, non-numeric port).
    """
    data: dict[str, Any] = {}
    if path.exists():
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            msg = f"config.toml: malformed TOML ({exc})"
            raise ConfigError(msg) from exc

    locale = data.get("locale", DEFAULT_LOCALE)
    if locale not in VALID_LOCALES:
        msg = f"locale: invalid value {locale!r} (expected one of {VALID_LOCALES})"
        raise ConfigError(msg)

    models_data = _section(data, "models")
    models = ModelsConfig(
        background=_require_str(
            "models",
            "background",
            models_data.get("background", DEFAULT_MODEL_BACKGROUND),
        ),
        mate=_require_str(
            "models", "mate", models_data.get("mate", DEFAULT_MODEL_MATE)
        ),
    )

    budget_data = _section(data, "budget")
    budget = BudgetConfig(
        soft_cap_usd=_require_number(
            "budget",
            "soft_cap_usd",
            budget_data.get("soft_cap_usd", DEFAULT_BUDGET_SOFT_CAP_USD),
        ),
        hard_cap_usd=_require_number(
            "budget",
            "hard_cap_usd",
            budget_data.get("hard_cap_usd", DEFAULT_BUDGET_HARD_CAP_USD),
        ),
    )

    world_data = _section(data, "world")
    world = WorldConfig(
        tick_ms=_require_int(
            "world", "tick_ms", world_data.get("tick_ms", DEFAULT_TICK_MS)
        ),
        day_length_real_minutes=_require_int(
            "world",
            "day_length_real_minutes",
            world_data.get("day_length_real_minutes", DEFAULT_DAY_LENGTH_REAL_MINUTES),
        ),
    )

    server_data = _section(data, "server")
    server = ServerConfig(
        port=_require_int(
            "server", "port", server_data.get("port", DEFAULT_SERVER_PORT)
        ),
    )

    logbook_data = _section(data, "logbook")
    logbook = LogbookConfig(
        weekly_summary=bool(logbook_data.get("weekly_summary", True)),
    )

    return Config(
        locale=locale,
        models=models,
        budget=budget,
        world=world,
        server=server,
        logbook=logbook,
    )
