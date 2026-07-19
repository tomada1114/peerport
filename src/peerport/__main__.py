"""CLI entry point: argument parsing and the full boot sequence.

Per `docs/design/architecture.md` §7, boot proceeds: load config → open DB
→ rotate backups → (persona/map loading is wired in by later issues
#11-#12) → start the FastAPI/uvicorn server. `--fresh` archives the
existing database before starting a brand-new world.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from peerport.config import Config, load_config
from peerport.db import (
    DEFAULT_BACKUP_KEEP,
    backup_db,
    open_db,
    reset_fresh,
    rotate_backups,
)
from peerport.errors import ConfigError
from peerport.server.app import create_app

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the `peerport` entry point.

    Args:
        argv: Argument list to parse; defaults to `sys.argv[1:]`.

    Returns:
        Namespace with `fresh: bool` and `debug: bool`.
    """
    parser = argparse.ArgumentParser(prog="peerport")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Archive the existing world and start a brand-new one.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging, including per-call LLM prompts/responses.",
    )
    return parser.parse_args(argv)


def _setup_logging(*, debug: bool) -> None:
    """Configure root logging verbosity for the process."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


def boot(
    *, fresh: bool, data_dir: Path, config_path: Path
) -> tuple[Config, sqlite3.Connection]:
    """Run the data-layer boot sequence: config, DB open, backup rotation.

    Args:
        fresh: If `True`, archive and empty the existing database.
        data_dir: Directory holding `peerport.db` and `backups/`.
        config_path: Location of `config.toml`.

    Returns:
        The resolved `Config` and an open, schema-applied DB connection.

    Raises:
        SystemExit: If `config.toml` is invalid; the reason is logged.
    """
    try:
        config = load_config(config_path)
    except ConfigError:
        logger.exception("invalid config.toml")
        raise SystemExit(1) from None

    db_path = data_dir / "peerport.db"
    backups_dir = data_dir / "backups"

    if fresh:
        reset_fresh(db_path, backups_dir)
    else:
        backup_db(db_path, backups_dir)

    conn = open_db(db_path)
    rotate_backups(backups_dir, keep=DEFAULT_BACKUP_KEEP)

    return config, conn


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point (console script `peerport`).

    Args:
        argv: Argument list to parse; defaults to `sys.argv[1:]`.

    Returns:
        Process exit code: `0` on success, non-zero on a boot failure.
    """
    args = parse_args(argv)
    _setup_logging(debug=args.debug)

    try:
        config, conn = boot(
            fresh=args.fresh,
            data_dir=Path("data"),
            config_path=Path("config.toml"),
        )
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1

    # DB persistence of live world state is wired in by #13+; #10 only
    # establishes the HTTP/WS server over an in-memory world.
    conn.close()

    app = create_app(config)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=config.server.port,
        log_level="debug" if args.debug else "info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
