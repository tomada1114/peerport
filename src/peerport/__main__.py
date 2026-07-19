"""CLI entry point: argument parsing and the full boot sequence.

Per `docs/design/architecture.md` §7, boot proceeds: load config → open DB
→ rotate backups → (persona/map loading is wired in by later issues
#11-#12) → start the FastAPI/uvicorn server. `--fresh` archives the
existing database before starting a brand-new world.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from peerport.config import Config, load_config
from peerport.db import (
    DEFAULT_BACKUP_KEEP,
    backup_db,
    load_world_seconds,
    open_db,
    reset_fresh,
    rotate_backups,
    save_world_seconds,
)
from peerport.errors import ConfigError, MapDataError, PersonaValidationError
from peerport.llm.budget import BudgetGuard
from peerport.llm.client import LLMClient, OpenAIStreamingTransport
from peerport.llm.prompts import build_fixed_prefix
from peerport.mate.chat import MateChat
from peerport.memory.stream import MemoryStream, OpenAIEmbedder
from peerport.peers.personas import load_personas
from peerport.server.app import create_app
from peerport.world.clock import WorldClock
from peerport.world.sim import Simulation
from peerport.world.worldmap import WorldMap

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from fastapi import FastAPI

    from peerport.peers.personas import Persona

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


def _wire_mate_chat(
    app: FastAPI,
    config: Config,
    conn: sqlite3.Connection,
    personas: dict[str, Persona],
    simulation: Simulation,
) -> None:
    """Attach the Mate chat pipeline when an API key is available.

    Without OPENAI_API_KEY the world still runs LLM-less (boot §7);
    /api/chat then answers 501 and the fog UI takes over (#27).
    """
    if not os.environ.get("OPENAI_API_KEY"):
        logger.info("OPENAI_API_KEY not set; Mate chat disabled (LLM-less world)")
        return
    mate = next((p for p in personas.values() if p.kind == "mate"), None)
    if mate is None:
        return
    budget = BudgetGuard(conn, config.budget.soft_cap_usd, config.budget.hard_cap_usd)
    llm = LLMClient(
        config=config,
        conn=conn,
        budget=budget,
        transport=OpenAIStreamingTransport(),
    )
    app.state.mate_chat = MateChat(
        llm=llm,
        memory=MemoryStream(conn, OpenAIEmbedder()),
        broadcaster=app.state.broadcaster,
        mate_id=mate.id,
        fixed_prefix=build_fixed_prefix(mate.body, config.locale),
        now_world=lambda: simulation.state.world_seconds,
    )


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

    try:
        personas = load_personas(Path("personas"))
    except PersonaValidationError:
        logger.exception("invalid persona file")
        conn.close()
        return 1
    try:
        worldmap = WorldMap.load(Path("data") / "map" / "port.json")
    except MapDataError:
        logger.exception("invalid map data")
        conn.close()
        return 1

    simulation = Simulation(
        worldmap=worldmap,
        personas=personas,
        rng=random.Random(),  # noqa: S311 -- sim wander randomness, not security
        clock=WorldClock(day_length_real_minutes=config.world.day_length_real_minutes),
        initial_world_seconds=load_world_seconds(conn),
    )

    app = create_app(config, simulation=simulation)
    _wire_mate_chat(app, config, conn, personas, simulation)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=config.server.port,
        log_level="debug" if args.debug else "info",
    )
    save_world_seconds(conn, simulation.state.world_seconds)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
