"""Acceptance-level tests for #27 (diegetic degraded states).

Covers, end to end with fakes and no network, the two REQUIRED
acceptance criteria: an LLM outage sets a `{"t": "state", "state":
"fog"}` frame over the wire while position (`diff`) frames keep
flowing from the simulation, and a hard-cap trip emits `{"t": "state",
"state": "hard_stop"}` and actually pauses the world (`Simulation.tick`
then yields no frames at all).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI

from peerport.__main__ import make_hard_cap_handler, make_outage_handler
from peerport.config import Config
from peerport.db import open_db
from peerport.errors import BudgetExceededError, LLMCallError
from peerport.llm.budget import BudgetGuard
from peerport.llm.client import (
    LLMClient,
    PromptParts,
    TransportReply,
    TransportUnavailableError,
)
from peerport.llm.outage import OutageTracker
from peerport.peers.personas import load_personas
from peerport.world.clock import WorldClock
from peerport.world.sim import Simulation
from peerport.world.worldmap import WorldMap
from tests.test_converse import FakeBroadcaster
from tests.test_llm_client import FakeTransport

if TYPE_CHECKING:
    import random
    import sqlite3
    from collections.abc import Callable, Iterator

REPO_ROOT = Path(__file__).parent.parent
TICK_MS = 500


@pytest.fixture
def anyio_backend() -> str:
    """Restrict anyio to the asyncio backend (matches the rest of the suite)."""
    return "asyncio"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A fresh on-disk DB per test."""
    connection = open_db(tmp_path / "degraded.db")
    yield connection
    connection.close()


def build_sim(rng: random.Random) -> Simulation:
    """A real `Simulation` over the repo's map/personas, for a genuine tick."""
    worldmap = WorldMap.load(REPO_ROOT / "data" / "map" / "port.json")
    personas = load_personas(REPO_ROOT / "personas")
    return Simulation(worldmap=worldmap, personas=personas, rng=rng, clock=WorldClock())


async def no_sleep(_seconds: float) -> None:
    """Backoff delay stand-in so failing calls don't actually wait."""
    return


def _outage_active(llm: LLMClient) -> bool:
    """Read `llm.outage.active` through a call boundary.

    Asserting the same `llm.outage.active` expression `is True` then
    later `is False` (or vice versa) makes mypy's attribute narrowing
    treat the second assert as unreachable (it doesn't see the
    intervening `await llm.call(...)` mutating the tracker); routing
    the read through this function's declared `bool` return type
    sidesteps that false positive.
    """
    assert llm.outage is not None
    return llm.outage.active


class TestOutageFogWhilePositionsContinue:
    """LLM outage sets `state`/`fog` while position diffs keep flowing (#27)."""

    @pytest.mark.anyio
    async def test_two_failed_calls_broadcast_fog_while_sim_keeps_ticking(
        self,
        conn: sqlite3.Connection,
        make_rng: Callable[[int], random.Random],
    ) -> None:
        app = FastAPI()
        app.state.broadcaster = FakeBroadcaster()
        sim = build_sim(make_rng(7))
        sim.assign_destination("tug", "pier_main")
        path_len = len(sim.peers["tug"].path)

        transport = FakeTransport([TransportUnavailableError("boom", status=503)] * 8)
        llm = LLMClient(
            config=Config(),
            conn=conn,
            budget=BudgetGuard(conn),
            transport=transport,
            sleep=no_sleep,
        )
        llm.outage = OutageTracker(on_change=make_outage_handler(app))

        # The world is moving before any outage - a real baseline to diff against.
        sim.tick(TICK_MS)
        tile_before_outage = sim.peers["tug"].tile

        # Two full failed dispatches (each exhausts its own retries) trip the
        # outage tracker - a single one must not (#27's boundary condition).
        with pytest.raises(LLMCallError):
            await llm.call(role="background", prompt=PromptParts("f", "v"))
        assert _outage_active(llm) is False

        with pytest.raises(LLMCallError):
            await llm.call(role="background", prompt=PromptParts("f", "v"))
        assert _outage_active(llm) is True

        # Position (diff) frames keep flowing while the outage is active -
        # the world only stops for a hard cap, never for an LLM outage.
        diffs = [sim.tick(TICK_MS) for _ in range(path_len - 1)]
        assert any(diff for diff in diffs)  # real diff frames, not empty ticks
        assert sim.peers["tug"].tile != tile_before_outage
        assert sim.paused is False

        await asyncio.sleep(0)  # flush the fire-and-forget broadcast
        broadcaster = app.state.broadcaster
        assert {
            "t": "state",
            "state": "fog",
            "active": True,
            "status": 503,
        } in broadcaster.frames

    @pytest.mark.anyio
    async def test_recovery_call_clears_fog_automatically(
        self, conn: sqlite3.Connection
    ) -> None:
        """REQ-004: fog clears on the next successful call, no Keeper action."""
        app = FastAPI()
        app.state.broadcaster = FakeBroadcaster()
        transport = FakeTransport(
            [TransportUnavailableError("boom", status=503)] * 8
            + [TransportReply(text="ok")]
        )
        llm = LLMClient(
            config=Config(),
            conn=conn,
            budget=BudgetGuard(conn),
            transport=transport,
            sleep=no_sleep,
        )
        llm.outage = OutageTracker(on_change=make_outage_handler(app))
        for _ in range(2):
            with pytest.raises(LLMCallError):
                await llm.call(role="background", prompt=PromptParts("f", "v"))
        assert _outage_active(llm) is True

        result = await llm.call(role="background", prompt=PromptParts("f", "v"))

        assert result.text == "ok"
        assert _outage_active(llm) is False
        await asyncio.sleep(0)
        assert {"t": "state", "state": "fog", "active": False} in (
            app.state.broadcaster.frames
        )


class TestHardCapPausesAndBroadcastsHardStop:
    """Hard-cap trip emits `state`/`hard_stop` and pauses the world (#27)."""

    @pytest.mark.anyio
    async def test_hard_cap_trip_pauses_sim_and_broadcasts_hard_stop(
        self,
        conn: sqlite3.Connection,
        make_rng: Callable[[int], random.Random],
    ) -> None:
        app = FastAPI()
        app.state.broadcaster = FakeBroadcaster()
        sim = build_sim(make_rng(3))
        sim.assign_destination("bell", "dock_square")

        # World moves normally before the cap trips.
        sim.tick(TICK_MS)
        tile_before_cap = sim.peers["bell"].tile

        budget = BudgetGuard(
            conn,
            soft_cap_usd=0.50,
            hard_cap_usd=2.00,
            on_hard_cap=make_hard_cap_handler(app, sim),
        )
        conn.execute(
            "INSERT INTO usage_log (ts_real, model, role, purpose, input_tokens,"
            " cached_tokens, output_tokens, est_cost_usd, status)"
            " VALUES (strftime('%s','now'), 'gpt-5-nano', 'background', 'seed',"
            " 0, 0, 0, 2.00, 'ok')"
        )
        conn.commit()

        with pytest.raises(BudgetExceededError):
            budget.check_hard_cap()

        assert sim.paused is True
        # A paused world advances neither the clock nor peer positions.
        assert sim.tick(TICK_MS) == []
        assert sim.peers["bell"].tile == tile_before_cap

        await asyncio.sleep(0)
        assert app.state.broadcaster.frames == [{"t": "state", "state": "hard_stop"}]

    @pytest.mark.anyio
    async def test_repeated_hard_cap_checks_pause_and_broadcast_only_once(
        self,
        conn: sqlite3.Connection,
        make_rng: Callable[[int], random.Random],
    ) -> None:
        app = FastAPI()
        app.state.broadcaster = FakeBroadcaster()
        sim = build_sim(make_rng(3))
        budget = BudgetGuard(
            conn,
            hard_cap_usd=2.00,
            on_hard_cap=make_hard_cap_handler(app, sim),
        )
        conn.execute(
            "INSERT INTO usage_log (ts_real, model, role, purpose, input_tokens,"
            " cached_tokens, output_tokens, est_cost_usd, status)"
            " VALUES (strftime('%s','now'), 'gpt-5-nano', 'background', 'seed',"
            " 0, 0, 0, 2.50, 'ok')"
        )
        conn.commit()

        for _ in range(3):
            with pytest.raises(BudgetExceededError):
                budget.check_hard_cap()

        await asyncio.sleep(0)
        assert app.state.broadcaster.frames == [{"t": "state", "state": "hard_stop"}]
