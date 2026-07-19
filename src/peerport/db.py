"""SQLite persistence: schema, backups, and write-failure tracking.

Uses stdlib `sqlite3` in WAL mode. One module owns all schema DDL, per
`docs/design/architecture.md` §3. Schema creation is idempotent — every
statement is `CREATE TABLE IF NOT EXISTS`, so it is always safe to call
`open_db()` on an existing database. Future issues (#16 `memories`, #17
`usage_log`) extend these tables via additional idempotent `ALTER TABLE`
statements appended to `_MIGRATIONS`, not by rewriting this file's base
schema.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from peerport.errors import DatabaseShutdownError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BACKUP_KEEP = 7
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS peers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        pair TEXT,
        sprite TEXT,
        pos_x INTEGER NOT NULL DEFAULT 0,
        pos_y INTEGER NOT NULL DEFAULT 0,
        state TEXT,
        mood TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relationships (
        peer_a TEXT NOT NULL,
        peer_b TEXT NOT NULL,
        score INTEGER NOT NULL DEFAULT 0,
        label TEXT,
        updated_ts INTEGER,
        PRIMARY KEY (peer_a, peer_b)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY,
        peer_id TEXT NOT NULL,
        ts_world INTEGER,
        ts_real INTEGER,
        kind TEXT,
        text TEXT,
        importance INTEGER,
        embedding BLOB,
        reflected INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS world_state (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY,
        ts_world INTEGER,
        ts_real INTEGER,
        type TEXT,
        actors TEXT,
        payload TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mails (
        id INTEGER PRIMARY KEY,
        friend_id TEXT,
        direction TEXT,
        subject TEXT,
        body TEXT,
        ts_real INTEGER,
        read INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS board_posts (
        id INTEGER PRIMARY KEY,
        author_id TEXT,
        body TEXT,
        ts_world INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_log (
        id INTEGER PRIMARY KEY,
        ts_real INTEGER,
        model TEXT,
        role TEXT,
        purpose TEXT,
        input_tokens INTEGER,
        cached_tokens INTEGER,
        output_tokens INTEGER,
        est_cost_usd REAL,
        status TEXT
    )
    """,
)

# Columns added after the original #9 schema shipped; applied idempotently
# so pre-existing worlds pick them up on the next boot (see #9 REQ-009).
_SCHEMA_UPGRADES = (
    ("usage_log", "role", "TEXT"),
    ("usage_log", "status", "TEXT"),
    ("relationships", "last_delta", "INTEGER"),
    ("board_posts", "created_at", "INTEGER"),
)


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open (creating if absent) the SQLite database and apply the schema.

    Args:
        db_path: Path to `peerport.db`. Parent directories are created as
            needed.

    Returns:
        An open connection in WAL mode with the full schema applied.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with conn:
        for statement in _MIGRATIONS:
            conn.execute(statement)
        for table, column, column_type in _SCHEMA_UPGRADES:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
    return conn


def _timestamp() -> str:
    """Return a collision-resistant timestamp for backup filenames."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S%f")


def backup_db(db_path: Path, backups_dir: Path) -> Path | None:
    """Copy the current database file into *backups_dir*.

    Args:
        db_path: Path to the live `peerport.db`.
        backups_dir: Directory to copy the backup into; created if absent.

    Returns:
        The path to the new backup file, or `None` if *db_path* did not
        exist (nothing to back up, e.g. on a brand-new install).
    """
    if not db_path.exists():
        return None
    backups_dir.mkdir(parents=True, exist_ok=True)
    dest = backups_dir / f"peerport-{_timestamp()}.db"
    dest.write_bytes(db_path.read_bytes())
    return dest


def rotate_backups(backups_dir: Path, keep: int = DEFAULT_BACKUP_KEEP) -> None:
    """Delete the oldest backups beyond the *keep* most recent generations.

    Args:
        backups_dir: Directory containing `peerport-*.db` backup files;
            created if absent.
        keep: Number of most recent backup generations to retain.
    """
    backups_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(backups_dir.glob("peerport-*.db"))
    excess = max(len(files) - keep, 0)
    for stale in files[:excess]:
        stale.unlink()


def reset_fresh(db_path: Path, backups_dir: Path) -> None:
    """Archive the existing database, then remove it for a brand-new world.

    Args:
        db_path: Path to the live `peerport.db`.
        backups_dir: Directory to archive the pre-reset database into.
    """
    backup_db(db_path, backups_dir)
    for suffix in ("", "-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        sidecar.unlink(missing_ok=True)


class Database:
    """Wraps a connection with consecutive-write-failure tracking.

    Per requirements.md §5.2: a single failed write is discarded and
    logged; five consecutive failures trigger a safe shutdown rather than
    letting the process silently drift out of sync with its own log.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        """Initialize the wrapper.

        Args:
            conn: An open SQLite connection.
            max_consecutive_failures: Consecutive failed writes before
                `DatabaseShutdownError` is raised.
        """
        self._conn = conn
        self._max_consecutive_failures = max_consecutive_failures
        self.consecutive_failures = 0

    def execute_write(self, sql: str, params: Sequence[object] = ()) -> None:
        """Execute a write statement, discarding it on failure.

        Args:
            sql: The SQL statement to execute.
            params: Bound parameters for the statement.

        Raises:
            DatabaseShutdownError: If this failure is the
                `max_consecutive_failures`-th in a row.
        """
        try:
            with self._conn:
                self._conn.execute(sql, params)
        except sqlite3.Error:
            self.consecutive_failures += 1
            logger.exception(
                "DB write failed (%d consecutive)", self.consecutive_failures
            )
            if self.consecutive_failures >= self._max_consecutive_failures:
                logger.exception("shutting down: consecutive DB write failures")
                msg = (
                    f"{self.consecutive_failures} consecutive DB write "
                    "failures; shutting down"
                )
                raise DatabaseShutdownError(msg) from None
        else:
            self.consecutive_failures = 0


WORLD_SECONDS_KEY = "world_seconds"


def load_world_seconds(conn: sqlite3.Connection) -> int:
    """Read the persisted world clock, or 0 for a brand-new world."""
    row = conn.execute(
        "SELECT value FROM world_state WHERE key = ?", (WORLD_SECONDS_KEY,)
    ).fetchone()
    return int(row[0]) if row is not None else 0


def save_world_seconds(conn: sqlite3.Connection, world_seconds: int) -> None:
    """Persist the world clock so it does not advance while stopped."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO world_state (key, value) VALUES (?, ?)",
            (WORLD_SECONDS_KEY, str(world_seconds)),
        )


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One LLM/embedding call's usage row (success or failure)."""

    model: str
    role: str
    purpose: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    est_cost_usd: float
    status: str
    ts_real: int | None = None


def insert_usage(conn: sqlite3.Connection, record: UsageRecord) -> None:
    """Record one LLM/embedding call in `usage_log`."""
    timestamp = (
        record.ts_real
        if record.ts_real is not None
        else int(datetime.now(UTC).timestamp())
    )
    with conn:
        conn.execute(
            "INSERT INTO usage_log (ts_real, model, role, purpose, input_tokens,"
            " cached_tokens, output_tokens, est_cost_usd, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                timestamp,
                record.model,
                record.role,
                record.purpose,
                record.input_tokens,
                record.cached_tokens,
                record.output_tokens,
                record.est_cost_usd,
                record.status,
            ),
        )


@dataclass(frozen=True, slots=True)
class Relationship:
    """A peer pair's relationship state (requirements §4.2)."""

    score: int = 0
    label: str = ""
    last_delta: int = 0


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def get_relationship(conn: sqlite3.Connection, a: str, b: str) -> Relationship:
    """Read the pair's relationship; a neutral default when none exists."""
    pa, pb = _pair_key(a, b)
    row = conn.execute(
        "SELECT score, label, last_delta FROM relationships"
        " WHERE peer_a = ? AND peer_b = ?",
        (pa, pb),
    ).fetchone()
    if row is None:
        return Relationship()
    return Relationship(score=row[0], label=row[1] or "", last_delta=row[2] or 0)


def save_relationship(
    conn: sqlite3.Connection, pair: tuple[str, str], relationship: Relationship
) -> None:
    """Upsert the pair's relationship (stored once with peer_a < peer_b)."""
    pa, pb = _pair_key(*pair)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO relationships"
            " (peer_a, peer_b, score, label, last_delta, updated_ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                pa,
                pb,
                relationship.score,
                relationship.label,
                relationship.last_delta,
                int(datetime.now(UTC).timestamp()),
            ),
        )


def list_relationships(
    conn: sqlite3.Connection, peer_id: str
) -> list[tuple[str, Relationship]]:
    """All relationships involving *peer_id* as (other_peer, relationship)."""
    rows = conn.execute(
        "SELECT peer_a, peer_b, score, label, last_delta FROM relationships"
        " WHERE peer_a = ? OR peer_b = ?",
        (peer_id, peer_id),
    ).fetchall()
    return [
        (
            pb if pa == peer_id else pa,
            Relationship(score=score, label=label or "", last_delta=delta or 0),
        )
        for pa, pb, score, label, delta in rows
    ]


def insert_event(
    conn: sqlite3.Connection,
    *,
    ts_world: int,
    kind: str,
    actors: list[str],
    payload: str,
) -> None:
    """Append one row to the full-history `events` table."""
    with conn:
        conn.execute(
            "INSERT INTO events (ts_world, ts_real, type, actors, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                ts_world,
                int(datetime.now(UTC).timestamp()),
                kind,
                json.dumps(actors),
                payload,
            ),
        )


def insert_board_post(
    conn: sqlite3.Connection,
    *,
    author_id: str,
    body: str,
    ts_world: int,
    created_at: int | None = None,
) -> int:
    """Insert one Signal Tower board post; returns the new post id."""
    timestamp = (
        created_at if created_at is not None else int(datetime.now(UTC).timestamp())
    )
    with conn:
        cursor = conn.execute(
            "INSERT INTO board_posts (author_id, body, ts_world, created_at)"
            " VALUES (?, ?, ?, ?)",
            (author_id, body, ts_world, timestamp),
        )
    return int(cursor.lastrowid or 0)


def list_board_posts(
    conn: sqlite3.Connection, limit: int | None = None
) -> list[dict[str, object]]:
    """Board posts, strictly newest first (flat list, no threading)."""
    sql = (
        "SELECT id, author_id, body, ts_world, created_at FROM board_posts"
        " ORDER BY created_at DESC, id DESC"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [
        {
            "id": row[0],
            "author_id": row[1],
            "body": row[2],
            "ts_world": row[3],
            "created_at": row[4],
        }
        for row in conn.execute(sql).fetchall()
    ]
