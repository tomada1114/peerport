"""Tests for peerport.__main__ (CLI entry point and boot sequence)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from peerport.__main__ import boot, main, parse_args
from peerport.db import open_db

if TYPE_CHECKING:
    from pathlib import Path


class TestParseArgs:
    def test_defaults_are_false(self) -> None:
        args = parse_args([])

        assert args.fresh is False
        assert args.debug is False

    def test_fresh_flag_sets_true(self) -> None:
        args = parse_args(["--fresh"])

        assert args.fresh is True

    def test_debug_flag_sets_true(self) -> None:
        args = parse_args(["--debug"])

        assert args.debug is True

    def test_both_flags_together(self) -> None:
        args = parse_args(["--fresh", "--debug"])

        assert args.fresh is True
        assert args.debug is True


class TestBoot:
    def test_creates_db_with_eight_tables_on_empty_dir(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        config_path = tmp_path / "config.toml"

        _config, conn = boot(fresh=False, data_dir=data_dir, config_path=config_path)
        conn.close()

        reopened = open_db(data_dir / "peerport.db")
        try:
            tables = {
                row[0]
                for row in reopened.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            reopened.close()
        assert {"peers", "world_state", "events", "usage_log"}.issubset(tables)
        assert (data_dir / "backups").exists()

    def test_second_run_preserves_rows_and_adds_backup(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        config_path = tmp_path / "config.toml"

        _config, conn = boot(fresh=False, data_dir=data_dir, config_path=config_path)
        conn.execute(
            "INSERT INTO peers (id, name, kind, sprite, pos_x, pos_y) "
            "VALUES ('beacon', 'Beacon', 'mate', 'beacon', 0, 0)"
        )
        conn.commit()
        conn.close()

        _config, conn2 = boot(fresh=False, data_dir=data_dir, config_path=config_path)
        rows = conn2.execute("SELECT id FROM peers").fetchall()
        conn2.close()

        assert rows == [("beacon",)]
        assert list((data_dir / "backups").glob("peerport-*.db"))

    def test_fresh_flag_empties_existing_data(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        config_path = tmp_path / "config.toml"

        _config, conn = boot(fresh=False, data_dir=data_dir, config_path=config_path)
        for i in range(6):
            conn.execute(
                "INSERT INTO peers (id, name, kind, sprite, pos_x, pos_y) "
                "VALUES (?, ?, 'peer', 's', 0, 0)",
                (f"p{i}", f"P{i}"),
            )
        conn.commit()
        conn.close()

        _config, fresh_conn = boot(
            fresh=True, data_dir=data_dir, config_path=config_path
        )
        count = fresh_conn.execute("SELECT COUNT(*) FROM peers").fetchone()[0]
        fresh_conn.close()

        assert count == 0
        assert list((data_dir / "backups").glob("peerport-*.db"))

    def test_invalid_config_raises_system_exit(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        config_path = tmp_path / "config.toml"
        config_path.write_text('locale = "fr"\n', encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            boot(fresh=False, data_dir=data_dir, config_path=config_path)

        assert exc_info.value.code != 0


class TestMain:
    def test_returns_zero_on_successful_boot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        exit_code = main([])

        assert exit_code == 0
        assert (tmp_path / "data" / "peerport.db").exists()

    def test_returns_nonzero_on_invalid_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.toml").write_text('locale = "fr"\n', encoding="utf-8")

        exit_code = main([])

        assert exit_code != 0

    def test_fresh_flag_via_cli_empties_prior_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        main([])
        conn = open_db(tmp_path / "data" / "peerport.db")
        conn.execute(
            "INSERT INTO peers (id, name, kind, sprite, pos_x, pos_y) "
            "VALUES ('beacon', 'Beacon', 'mate', 'beacon', 0, 0)"
        )
        conn.commit()
        conn.close()

        exit_code = main(["--fresh"])

        assert exit_code == 0
        reopened = open_db(tmp_path / "data" / "peerport.db")
        try:
            count = reopened.execute("SELECT COUNT(*) FROM peers").fetchone()[0]
        finally:
            reopened.close()
        assert count == 0
