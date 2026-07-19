"""Tests for peerport.db."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from peerport.db import Database, backup_db, open_db, reset_fresh, rotate_backups
from peerport.errors import DatabaseShutdownError

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

EXPECTED_TABLES = {
    "peers",
    "relationships",
    "memories",
    "world_state",
    "events",
    "mails",
    "board_posts",
    "usage_log",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


class TestOpenDbSchema:
    def test_creates_all_eight_tables_on_empty_dir(self, tmp_path: Path) -> None:
        db_path = tmp_path / "peerport.db"

        conn = open_db(db_path)
        try:
            assert EXPECTED_TABLES.issubset(_table_names(conn))
        finally:
            conn.close()

    def test_is_idempotent_across_two_opens(self, tmp_path: Path) -> None:
        db_path = tmp_path / "peerport.db"

        conn1 = open_db(db_path)
        conn1.execute(
            "INSERT INTO peers (id, name, kind, sprite, pos_x, pos_y) "
            "VALUES ('beacon', 'Beacon', 'mate', 'beacon', 0, 0)"
        )
        conn1.commit()
        conn1.close()

        conn2 = open_db(db_path)
        try:
            rows = conn2.execute("SELECT id FROM peers").fetchall()
            assert rows == [("beacon",)]
        finally:
            conn2.close()

    def test_peers_table_has_required_columns(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "peerport.db")
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(peers)")}
        finally:
            conn.close()
        assert {"id", "name", "kind", "sprite", "pos_x", "pos_y"}.issubset(columns)

    def test_world_state_table_has_key_value_columns(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "peerport.db")
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(world_state)")}
        finally:
            conn.close()
        assert columns == {"key", "value"}

    def test_events_table_has_required_columns(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "peerport.db")
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        finally:
            conn.close()
        assert {"id", "ts_world", "ts_real", "type", "payload"}.issubset(columns)

    def test_enables_wal_journal_mode(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "peerport.db")
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert mode.lower() == "wal"


class TestBackupDb:
    def test_no_backup_when_db_file_absent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "peerport.db"
        backups_dir = tmp_path / "backups"

        result = backup_db(db_path, backups_dir)

        assert result is None
        assert not backups_dir.exists() or list(backups_dir.iterdir()) == []

    def test_copies_existing_db_into_backups_dir(self, tmp_path: Path) -> None:
        db_path = tmp_path / "peerport.db"
        backups_dir = tmp_path / "backups"
        conn = open_db(db_path)
        conn.close()

        result = backup_db(db_path, backups_dir)

        assert result is not None
        assert result.exists()
        assert result.parent == backups_dir


class TestRotateBackups:
    def test_rotation_keeps_exactly_seven_of_eight(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        for i in range(8):
            (backups_dir / f"peerport-2026070{i}-000000000000.db").write_bytes(b"x")

        rotate_backups(backups_dir, keep=7)

        remaining = sorted(backups_dir.iterdir())
        assert len(remaining) == 7
        assert "peerport-20260700-000000000000.db" not in {p.name for p in remaining}

    def test_rotation_is_noop_under_the_cap(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        (backups_dir / "peerport-20260701-000000000000.db").write_bytes(b"x")

        rotate_backups(backups_dir, keep=7)

        assert len(list(backups_dir.iterdir())) == 1

    @pytest.mark.parametrize("count", [2, 3, 4, 5, 6])
    def test_rotation_keeps_all_files_when_under_cap(
        self, tmp_path: Path, count: int
    ) -> None:
        """Regression test.

        `len(files) - keep` going negative must not wrap around and delete
        files via Python's negative-slice semantics.
        """
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        names = [f"peerport-2026070{i}-000000000000.db" for i in range(count)]
        for name in names:
            (backups_dir / name).write_bytes(b"x")

        rotate_backups(backups_dir, keep=7)

        remaining = {p.name for p in backups_dir.iterdir()}
        assert remaining == set(names)

    def test_rotation_on_missing_dir_creates_it_empty(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"

        rotate_backups(backups_dir, keep=7)

        assert backups_dir.exists()
        assert list(backups_dir.iterdir()) == []


class TestResetFresh:
    def test_backs_up_and_empties_existing_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "peerport.db"
        backups_dir = tmp_path / "backups"
        conn = open_db(db_path)
        for i in range(6):
            conn.execute(
                "INSERT INTO peers (id, name, kind, sprite, pos_x, pos_y) "
                "VALUES (?, ?, 'peer', 's', 0, 0)",
                (f"p{i}", f"P{i}"),
            )
        conn.commit()
        conn.close()

        reset_fresh(db_path, backups_dir)

        assert list(backups_dir.glob("peerport-*.db")), "expected a backup file"
        new_conn = open_db(db_path)
        try:
            count = new_conn.execute("SELECT COUNT(*) FROM peers").fetchone()[0]
            assert count == 0
        finally:
            new_conn.close()

    def test_on_missing_db_just_creates_fresh_schema(self, tmp_path: Path) -> None:
        db_path = tmp_path / "peerport.db"
        backups_dir = tmp_path / "backups"

        reset_fresh(db_path, backups_dir)

        conn = open_db(db_path)
        try:
            assert EXPECTED_TABLES.issubset(_table_names(conn))
        finally:
            conn.close()


class TestDatabaseWriteFailureHandling:
    def test_single_write_failure_is_discarded_without_raising(
        self, tmp_path: Path
    ) -> None:
        conn = open_db(tmp_path / "peerport.db")
        db = Database(conn)

        db.execute_write("INSERT INTO nonexistent_table (x) VALUES (1)")

        assert db.consecutive_failures == 1
        conn.close()

    def test_successful_write_resets_failure_counter(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "peerport.db")
        db = Database(conn)

        db.execute_write("INSERT INTO nonexistent_table (x) VALUES (1)")
        db.execute_write(
            "INSERT INTO peers (id, name, kind, sprite, pos_x, pos_y) "
            "VALUES ('beacon', 'Beacon', 'mate', 'beacon', 0, 0)"
        )

        assert db.consecutive_failures == 0
        conn.close()

    def test_fifth_consecutive_failure_raises_shutdown_error(
        self, tmp_path: Path
    ) -> None:
        conn = open_db(tmp_path / "peerport.db")
        db = Database(conn, max_consecutive_failures=5)

        for _ in range(4):
            db.execute_write("INSERT INTO nonexistent_table (x) VALUES (1)")

        with pytest.raises(DatabaseShutdownError):
            db.execute_write("INSERT INTO nonexistent_table (x) VALUES (1)")

        conn.close()
