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
    ("mails", "parent_id", "INTEGER"),
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


LAST_SHUTDOWN_KEY = "last_shutdown_ts_real"


def load_last_shutdown_ts_real(conn: sqlite3.Connection) -> int | None:
    """Read the wall-clock shutdown time, or `None` on a brand-new world."""
    row = conn.execute(
        "SELECT value FROM world_state WHERE key = ?", (LAST_SHUTDOWN_KEY,)
    ).fetchone()
    return int(row[0]) if row is not None else None


def save_last_shutdown_ts_real(conn: sqlite3.Connection, ts_real: int) -> None:
    """Persist the wall-clock shutdown time for the next boot's absence check."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO world_state (key, value) VALUES (?, ?)",
            (LAST_SHUTDOWN_KEY, str(ts_real)),
        )


def get_world_state(conn: sqlite3.Connection, key: str) -> str | None:
    """Read a raw `world_state` value by key, or `None` if absent."""
    row = conn.execute("SELECT value FROM world_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else None


def set_world_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a raw `world_state` value by key."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO world_state (key, value) VALUES (?, ?)",
            (key, value),
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


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One `events` table row (full-history log)."""

    ts_world: int
    kind: str
    actors: list[str]
    payload: str
    ts_real: int | None = None


def insert_event(conn: sqlite3.Connection, record: EventRecord) -> None:
    """Append one row to the full-history `events` table.

    Args:
        conn: Open database connection.
        record: The event to append. A `None` `ts_real` defaults to now;
            callers writing several rows for one logical batch (e.g. one
            logbook generation) may pass the same `ts_real` to group them.
    """
    timestamp = (
        record.ts_real
        if record.ts_real is not None
        else int(datetime.now(UTC).timestamp())
    )
    with conn:
        conn.execute(
            "INSERT INTO events (ts_world, ts_real, type, actors, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                record.ts_world,
                timestamp,
                record.kind,
                json.dumps(record.actors),
                record.payload,
            ),
        )


def list_events_by_type(
    conn: sqlite3.Connection, event_type: str
) -> list[dict[str, object]]:
    """All `events` rows of *event_type*, oldest first."""
    rows = conn.execute(
        "SELECT id, ts_world, ts_real, actors, payload FROM events"
        " WHERE type = ? ORDER BY id",
        (event_type,),
    ).fetchall()
    return [
        {
            "id": row[0],
            "ts_world": row[1],
            "ts_real": row[2],
            "actors": json.loads(row[3] or "[]"),
            "payload": json.loads(row[4]),
        }
        for row in rows
    ]


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


@dataclass(frozen=True, slots=True)
class Mail:
    """One stored mail row: a friend's letter or a Keeper reply."""

    id: int
    friend_id: str
    direction: str
    subject: str
    body: str
    ts_real: int
    read: bool
    parent_id: int | None = None


@dataclass(frozen=True, slots=True)
class NewMail:
    """Fields for inserting one new `mails` row."""

    friend_id: str
    direction: str
    subject: str
    body: str
    parent_id: int | None = None
    ts_real: int | None = None


def _row_to_mail(row: tuple[object, ...]) -> Mail:
    return Mail(
        id=row[0],  # type: ignore[arg-type]
        friend_id=row[1],  # type: ignore[arg-type]
        direction=row[2],  # type: ignore[arg-type]
        subject=row[3],  # type: ignore[arg-type]
        body=row[4],  # type: ignore[arg-type]
        ts_real=row[5],  # type: ignore[arg-type]
        read=bool(row[6]),
        parent_id=row[7],  # type: ignore[arg-type]
    )


def insert_mail(conn: sqlite3.Connection, mail: NewMail) -> int:
    """Insert one mail row (a friend's letter or a Keeper reply).

    Returns:
        The new row id.
    """
    timestamp = (
        mail.ts_real if mail.ts_real is not None else int(datetime.now(UTC).timestamp())
    )
    with conn:
        cursor = conn.execute(
            "INSERT INTO mails (friend_id, direction, subject, body, ts_real,"
            " read, parent_id) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (
                mail.friend_id,
                mail.direction,
                mail.subject,
                mail.body,
                timestamp,
                mail.parent_id,
            ),
        )
    return int(cursor.lastrowid or 0)


def list_mails(conn: sqlite3.Connection) -> list[Mail]:
    """All mails, newest first."""
    rows = conn.execute(
        "SELECT id, friend_id, direction, subject, body, ts_real, read, parent_id"
        " FROM mails ORDER BY ts_real DESC, id DESC"
    ).fetchall()
    return [_row_to_mail(row) for row in rows]


def get_mail(conn: sqlite3.Connection, mail_id: int) -> Mail | None:
    """One mail row by id, or `None` if it does not exist."""
    row = conn.execute(
        "SELECT id, friend_id, direction, subject, body, ts_real, read, parent_id"
        " FROM mails WHERE id = ?",
        (mail_id,),
    ).fetchone()
    return _row_to_mail(row) if row is not None else None


def mark_mail_read(conn: sqlite3.Connection, mail_id: int) -> None:
    """Mark one mail as read (clears its unread dot)."""
    with conn:
        conn.execute("UPDATE mails SET read = 1 WHERE id = ?", (mail_id,))
