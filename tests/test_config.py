"""Tests for peerport.config."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from peerport.config import load_config

from peerport.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path


class TestLoadConfigDefaults:
    def test_missing_file_yields_all_defaults(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "config.toml")

        assert config.locale == "en"
        assert config.models.background == "gpt-5-nano"
        assert config.models.mate == "gpt-5-mini"
        assert config.budget.soft_cap_usd == pytest.approx(0.50)
        assert config.budget.hard_cap_usd == pytest.approx(2.00)
        assert config.world.tick_ms == 500
        assert config.world.day_length_real_minutes == 120
        assert config.server.port == 8712

    def test_empty_file_yields_all_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("", encoding="utf-8")

        config = load_config(path)

        assert config.locale == "en"
        assert config.models.background == "gpt-5-nano"
        assert config.models.mate == "gpt-5-mini"
        assert config.budget.soft_cap_usd == pytest.approx(0.50)
        assert config.budget.hard_cap_usd == pytest.approx(2.00)
        assert config.world.tick_ms == 500
        assert config.world.day_length_real_minutes == 120
        assert config.server.port == 8712


class TestLoadConfigPartial:
    def test_partial_file_resolves_remaining_fields_to_defaults(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[server]\nport = 9000\n", encoding="utf-8")

        config = load_config(path)

        assert config.server.port == 9000
        assert config.locale == "en"
        assert config.models.background == "gpt-5-nano"
        assert config.models.mate == "gpt-5-mini"
        assert config.budget.soft_cap_usd == pytest.approx(0.50)
        assert config.budget.hard_cap_usd == pytest.approx(2.00)
        assert config.world.tick_ms == 500
        assert config.world.day_length_real_minutes == 120

    def test_partial_file_overrides_only_locale(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('locale = "ja"\n', encoding="utf-8")

        config = load_config(path)

        assert config.locale == "ja"
        assert config.server.port == 8712


class TestLoadConfigValidation:
    def test_invalid_locale_raises_config_error_naming_field_and_value(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.toml"
        path.write_text('locale = "fr"\n', encoding="utf-8")

        with pytest.raises(ConfigError, match=r"locale.*fr"):
            load_config(path)

    def test_non_numeric_port_raises_config_error(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('[server]\nport = "not-a-number"\n', encoding="utf-8")

        with pytest.raises(ConfigError, match=r"server\.port"):
            load_config(path)

    def test_non_numeric_tick_ms_raises_config_error(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('[world]\ntick_ms = "fast"\n', encoding="utf-8")

        with pytest.raises(ConfigError, match=r"world\.tick_ms"):
            load_config(path)

    def test_malformed_toml_raises_config_error(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("this is not [valid toml", encoding="utf-8")

        with pytest.raises(ConfigError):
            load_config(path)
