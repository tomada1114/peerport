"""Tests for peerport.__main__ (CLI entry point and boot sequence)."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI

from peerport.__main__ import (
    boot,
    main,
    make_hard_cap_handler,
    make_outage_handler,
    parse_args,
)
from peerport.db import open_db
from tests.test_converse import FakeBroadcaster

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def world_files(tmp_path: Path) -> None:
    """Copy the repo personas and map into the test cwd for full boots."""
    shutil.copytree(REPO_ROOT / "personas", tmp_path / "personas")
    (tmp_path / "data" / "map").mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "data" / "map" / "port.json",
        tmp_path / "data" / "map" / "port.json",
    )


@pytest.fixture(autouse=True)
def uvicorn_run_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Mock `uvicorn.run` file-wide.

    `uvicorn.run()` opens a real network listener and blocks forever, so
    every test that calls `main()` needs it mocked — per the testing
    rules, boundaries like network I/O are exactly what should be
    mocked, not the unit under test. Autouse + module-scoped so every
    class in this file gets it, not just `TestMain`.
    """
    calls: list[dict[str, object]] = []

    def _fake_run(app: object, **kwargs: object) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr("peerport.__main__.uvicorn.run", _fake_run)
    return calls


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
    """Tests covering the full CLI boot sequence.

    `uvicorn.run()` is mocked file-wide by the `uvicorn_run_calls`
    autouse fixture above.
    """

    @pytest.mark.usefixtures("world_files")
    def test_returns_zero_on_successful_boot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        exit_code = main([])

        assert exit_code == 0
        assert (tmp_path / "data" / "peerport.db").exists()

    @pytest.mark.usefixtures("world_files")
    def test_notes_store_wired_without_api_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        uvicorn_run_calls: list[dict[str, object]],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        main([])

        app = cast("FastAPI", uvicorn_run_calls[0]["app"])
        assert app.state.notes_store is not None

    def test_returns_nonzero_on_invalid_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.toml").write_text('locale = "fr"\n', encoding="utf-8")

        exit_code = main([])

        assert exit_code != 0

    @pytest.mark.usefixtures("world_files")
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

    @pytest.mark.usefixtures("world_files")
    def test_starts_uvicorn_on_the_configured_port(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        uvicorn_run_calls: list[dict[str, object]],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.toml").write_text(
            "[server]\nport = 9001\n", encoding="utf-8"
        )

        main([])

        assert len(uvicorn_run_calls) == 1
        assert uvicorn_run_calls[0]["port"] == 9001

    def test_returns_nonzero_when_map_data_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        shutil.copytree(REPO_ROOT / "personas", tmp_path / "personas")

        exit_code = main([])

        assert exit_code == 1

    @pytest.mark.usefixtures("world_files")
    def test_world_clock_persisted_after_shutdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        main([])

        conn = open_db(tmp_path / "data" / "peerport.db")
        try:
            row = conn.execute(
                "SELECT value FROM world_state WHERE key = 'world_seconds'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None

    @pytest.mark.usefixtures("world_files")
    def test_last_shutdown_ts_real_persisted_after_shutdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        before = int(time.time())
        main([])
        after = int(time.time())

        conn = open_db(tmp_path / "data" / "peerport.db")
        try:
            row = conn.execute(
                "SELECT value FROM world_state WHERE key = 'last_shutdown_ts_real'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert before <= int(row[0]) <= after

    @pytest.mark.usefixtures("world_files")
    def test_uvicorn_run_exception_still_persists_world_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash inside uvicorn.run() must not lose world state (finding).

        Before the fix, save_world_seconds/save_last_shutdown_ts_real
        only ran after uvicorn.run() returned normally, so a startup
        exception (e.g. the configured port already in use) silently
        dropped however much world time had accrued.
        """

        def _raise_run(app: object, **kwargs: object) -> None:
            message = "port already in use"
            raise OSError(message)

        monkeypatch.setattr("peerport.__main__.uvicorn.run", _raise_run)
        monkeypatch.chdir(tmp_path)

        before = int(time.time())
        with pytest.raises(OSError, match="port already in use"):
            main([])
        after = int(time.time())

        conn = open_db(tmp_path / "data" / "peerport.db")
        try:
            world_seconds_row = conn.execute(
                "SELECT value FROM world_state WHERE key = 'world_seconds'"
            ).fetchone()
            shutdown_row = conn.execute(
                "SELECT value FROM world_state WHERE key = 'last_shutdown_ts_real'"
            ).fetchone()
        finally:
            conn.close()
        assert world_seconds_row is not None
        assert shutdown_row is not None
        assert before <= int(shutdown_row[0]) <= after


class TestDegradedStateWiring:
    """#27: the shared outage tracker and hard-cap signal.

    Both must reach every LLM-gated engine `main()` wires. Pre-existing
    bug found while writing this test (unrelated to #27, not fixed -
    see the report): `_wire_mate_chat` and `_wire_peer_society`
    construct `MateChat`/`ConversationEngine` with
    `broadcaster=ctx.app.state.broadcaster` evaluated eagerly, but the
    app's lifespan (which sets `app.state.broadcaster`) only runs once
    uvicorn actually starts serving - well after `main()`'s `_wire_*`
    calls, so `main()` crashes at `_wire_mate_chat` whenever
    OPENAI_API_KEY is set, before ever reaching `_wire_friends` or
    `_wire_logbook`. The two broken helpers are stubbed out here (like
    this file already stubs `uvicorn.run`) so `main()` can still be
    driven end-to-end to prove the #27 wiring on the two helpers that
    bug doesn't affect.
    """

    @pytest.mark.usefixtures("world_files")
    def test_outage_and_hard_cap_shared_across_engines(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        uvicorn_run_calls: list[dict[str, object]],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        monkeypatch.setattr("peerport.__main__._wire_mate_chat", lambda _ctx: None)
        monkeypatch.setattr("peerport.__main__._wire_peer_society", lambda _ctx: None)

        main([])

        app = cast("FastAPI", uvicorn_run_calls[0]["app"])
        mail_llm = app.state.mail_service.llm
        logbook_llm = app.state.logbook_service.llm

        assert mail_llm.outage is not None
        assert mail_llm.outage is logbook_llm.outage

        assert mail_llm.budget.on_hard_cap is not None
        assert mail_llm.budget.on_hard_cap is logbook_llm.budget.on_hard_cap

    @pytest.mark.usefixtures("world_files")
    def test_no_outage_or_hard_cap_wired_without_api_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        exit_code = main([])

        assert exit_code == 0


class TestDegradedStateHandlers:
    """Direct tests of `make_outage_handler`/`make_hard_cap_handler` (#27)."""

    @pytest.fixture
    def anyio_backend(self) -> str:
        return "asyncio"

    @pytest.fixture
    def app(self) -> FastAPI:
        bare_app = FastAPI()
        bare_app.state.broadcaster = FakeBroadcaster()
        return bare_app

    @pytest.mark.anyio
    async def test_outage_handler_broadcasts_fog_frame_with_status(
        self, app: FastAPI
    ) -> None:
        on_change = make_outage_handler(app)

        on_change(True, 503)
        await asyncio.sleep(0)

        broadcaster = cast("FakeBroadcaster", app.state.broadcaster)
        assert broadcaster.frames == [
            {"t": "state", "state": "fog", "active": True, "status": 503}
        ]

    @pytest.mark.anyio
    async def test_outage_handler_clears_fog_without_status(self, app: FastAPI) -> None:
        on_change = make_outage_handler(app)

        on_change(False, None)
        await asyncio.sleep(0)

        broadcaster = cast("FakeBroadcaster", app.state.broadcaster)
        assert broadcaster.frames == [{"t": "state", "state": "fog", "active": False}]

    @pytest.mark.anyio
    async def test_hard_cap_handler_pauses_and_broadcasts_once(
        self, app: FastAPI
    ) -> None:
        class FakeSimulation:
            """Minimal stand-in exposing only what the handler touches."""

            def __init__(self) -> None:
                self.paused = False

        simulation_double = FakeSimulation()
        on_hard_cap = make_hard_cap_handler(app, simulation_double)  # type: ignore[arg-type]

        on_hard_cap()
        await asyncio.sleep(0)

        broadcaster = cast("FakeBroadcaster", app.state.broadcaster)
        assert simulation_double.paused is True
        assert broadcaster.frames == [{"t": "state", "state": "hard_stop"}]

    @pytest.mark.anyio
    async def test_hard_cap_handler_is_idempotent_once_paused(
        self, app: FastAPI
    ) -> None:
        class FakeSimulation:
            """Minimal stand-in exposing only what the handler touches."""

            def __init__(self) -> None:
                self.paused = False

        simulation_double = FakeSimulation()
        on_hard_cap = make_hard_cap_handler(app, simulation_double)  # type: ignore[arg-type]

        on_hard_cap()
        on_hard_cap()
        on_hard_cap()
        await asyncio.sleep(0)

        broadcaster = cast("FakeBroadcaster", app.state.broadcaster)
        assert len(broadcaster.frames) == 1


class TestReflectionWiring:
    """#26: the reflection/forgetting engine is wired at boot.

    Reuses the same `_wire_mate_chat`/`_wire_peer_society` stubbing as
    `TestDegradedStateWiring` above (see that class's docstring for the
    pre-existing, unrelated boot-ordering bug those two helpers hit).
    """

    @pytest.mark.usefixtures("world_files")
    def test_reflection_engine_wired_and_shares_outage_and_hard_cap(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        uvicorn_run_calls: list[dict[str, object]],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        monkeypatch.setattr("peerport.__main__._wire_mate_chat", lambda _ctx: None)
        monkeypatch.setattr("peerport.__main__._wire_peer_society", lambda _ctx: None)

        main([])

        app = cast("FastAPI", uvicorn_run_calls[0]["app"])
        reflection_llm = app.state.reflection_engine.llm
        logbook_llm = app.state.logbook_service.llm

        assert reflection_llm.outage is not None
        assert reflection_llm.outage is logbook_llm.outage
        assert reflection_llm.budget.on_hard_cap is not None
        assert reflection_llm.budget.on_hard_cap is logbook_llm.budget.on_hard_cap

    @pytest.mark.usefixtures("world_files")
    def test_reflection_engine_not_wired_without_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        exit_code = main([])

        assert exit_code == 0


class TestMemoryScoringWiring:
    """Finding: nothing ever scheduled periodic importance scoring.

    Reuses the same `_wire_mate_chat`/`_wire_peer_society` stubbing as
    `TestReflectionWiring` above.
    """

    @pytest.mark.usefixtures("world_files")
    def test_memory_scoring_wired_and_shares_outage_and_hard_cap(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        uvicorn_run_calls: list[dict[str, object]],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
        monkeypatch.setattr("peerport.__main__._wire_mate_chat", lambda _ctx: None)
        monkeypatch.setattr("peerport.__main__._wire_peer_society", lambda _ctx: None)

        main([])

        app = cast("FastAPI", uvicorn_run_calls[0]["app"])
        scoring_llm = app.state.memory_scoring_llm
        logbook_llm = app.state.logbook_service.llm

        assert app.state.memory_scoring_memory is not None
        assert scoring_llm.outage is not None
        assert scoring_llm.outage is logbook_llm.outage
        assert scoring_llm.budget.on_hard_cap is not None
        assert scoring_llm.budget.on_hard_cap is logbook_llm.budget.on_hard_cap

    @pytest.mark.usefixtures("world_files")
    def test_memory_scoring_not_wired_without_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        exit_code = main([])

        assert exit_code == 0
