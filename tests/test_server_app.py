"""Tests for peerport.server.app (app factory, static hosting, lifespan)."""

from __future__ import annotations

import asyncio
import contextlib
import random
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from peerport.config import Config, ServerConfig
from peerport.llm.prompts import LogbookEvent
from peerport.peers.personas import load_personas
from peerport.server.app import _tick_loop, _tick_loop_simulation, create_app
from peerport.server.state import WorldState
from peerport.world.sim import Simulation
from peerport.world.worldmap import WorldMap
from tests.test_converse import FakeBroadcaster

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from peerport.server.state import Broadcaster

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio only (matches the rest of the suite)."""
    return "asyncio"


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse every `asyncio.sleep` call to one real event-loop turn.

    The tick/reflection/mail driver loops sleep for real intervals
    (tick_ms, 30s, 1800s, 300s) before each iteration; patching the
    shared `asyncio` module object lets a loop spin through several
    iterations inside a single fast test instead of actually waiting.
    """
    real_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    async def _fast_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)


class TestIndexPage:
    def test_root_returns_200_html(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_root_references_net_js(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            response = client.get("/")

        assert "net.js" in response.text


class TestVendoredStaticAssets:
    def test_pixi_min_js_returns_200_from_repo(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            response = client.get("/static/vendor/pixi.min.js")

        assert response.status_code == 200
        assert b"pixi.js" in response.content[:200]

    def test_net_js_is_served(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            response = client.get("/static/js/net.js")

        assert response.status_code == 200
        assert "WebSocket" in response.text


class TestAppFactoryDefaults:
    def test_uses_default_config_when_none_given(self) -> None:
        app = create_app()
        with TestClient(app):
            assert app.state.config.server.port == 8712

    def test_honors_a_supplied_config(self) -> None:
        app = create_app(Config(server=ServerConfig(port=9999)))
        with TestClient(app):
            assert app.state.config.server.port == 9999

    def test_lifespan_starts_and_stops_cleanly(self) -> None:
        app = create_app()
        with TestClient(app):
            assert app.state.world_state is not None
        # No assertion beyond "no exception raised on teardown".


class FakeLogbookService:
    def __init__(self) -> None:
        self.events = [LogbookEvent(peer_ids=["tug"], text="Tug tidied the pier.")]

    async def maybe_generate_absence_report(self) -> list[LogbookEvent]:
        # Real generation is one LLM round trip; a short delay here mirrors
        # that boundary latency so the boot task cannot outrace the test's
        # own WS handshake (a real network call never would).
        await asyncio.sleep(0.05)
        return self.events

    async def maybe_generate_weekly_summary(
        self,
        *,
        enabled: bool,  # noqa: ARG002 -- fake matches the real service's signature
    ) -> list[LogbookEvent]:
        return []

    def digest_text(self, events: list[LogbookEvent]) -> str:
        return f"Welcome back. While you were away... {events[0].text}"


class TestLogbookBoot:
    def test_boot_broadcasts_digest_and_logbook_updated_over_ws(self) -> None:
        app = create_app()
        app.state.logbook_service = FakeLogbookService()
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # snapshot
            frames = [ws.receive_json() for _ in range(2)]

        types = {frame["t"] for frame in frames}
        assert "digest" in types
        assert any(
            frame["t"] == "event" and frame["kind"] == "logbook_updated"
            for frame in frames
        )
        digest_frame = next(frame for frame in frames if frame["t"] == "digest")
        assert "Tug tidied the pier." in digest_frame["text"]


class TestMailBroadcasterWiring:
    def test_broadcaster_exists_before_lifespan_and_survives_startup(self) -> None:
        # The `_wire_*` helpers in __main__.py run before uvicorn starts
        # serving, so `app.state.broadcaster` must exist as soon as
        # create_app() returns, and the lifespan must keep (not replace)
        # that instance or every service wired at boot would publish into
        # a dead broadcaster.
        app = create_app()
        boot_broadcaster = app.state.broadcaster
        assert boot_broadcaster is not None
        with TestClient(app):
            assert app.state.broadcaster is boot_broadcaster


class TestReflectionBoot:
    """#26: the reflection/forgetting polling loops start alongside the rest.

    `run_reflection_loop`/`run_forgetting_loop` sleep 30s/1800s before
    ever touching the engine (`# pragma: no cover - async driver` in
    reflect.py), so - like `test_lifespan_starts_and_stops_cleanly` above
    - this only proves the tasks are created and cancelled without error,
    not that a reflection actually runs within the test.
    """

    def test_reflection_loops_start_and_stop_cleanly(self) -> None:
        worldmap = WorldMap.load(REPO_ROOT / "data" / "map" / "port.json")
        personas = load_personas(REPO_ROOT / "personas")
        simulation = Simulation(
            worldmap=worldmap,
            personas=personas,
            rng=random.Random(0),  # noqa: S311 -- test seed, not security
        )
        app = create_app(simulation=simulation)
        app.state.reflection_engine = object()

        with TestClient(app):
            pass  # no assertion beyond "no exception raised on teardown"

    def test_no_reflection_tasks_without_a_reflection_engine(self) -> None:
        app = create_app()

        with TestClient(app):
            assert getattr(app.state, "reflection_engine", None) is None


class TestMemoryScoringBoot:
    """Finding: nothing ever scheduled periodic importance scoring.

    Mirrors `TestReflectionBoot` above: `run_scoring_loop` sleeps
    `SCORING_CHECK_INTERVAL_SECONDS` before ever touching its arguments,
    so this only proves the task is created/cancelled cleanly, not that
    a scoring pass actually runs within the test.
    """

    def test_scoring_loop_starts_and_stops_cleanly(self) -> None:
        worldmap = WorldMap.load(REPO_ROOT / "data" / "map" / "port.json")
        personas = load_personas(REPO_ROOT / "personas")
        simulation = Simulation(
            worldmap=worldmap,
            personas=personas,
            rng=random.Random(0),  # noqa: S311 -- test seed, not security
        )
        app = create_app(simulation=simulation)
        app.state.personas = personas
        app.state.memory_scoring_llm = object()
        app.state.memory_scoring_memory = object()

        with TestClient(app):
            pass  # no assertion beyond "no exception raised on teardown"

    def test_no_scoring_tasks_without_being_wired(self) -> None:
        app = create_app()

        with TestClient(app):
            assert getattr(app.state, "memory_scoring_llm", None) is None


class TestTickLoopResilience:
    """Finding: a single bad tick used to kill the loop forever.

    Neither `_tick_loop` nor `_tick_loop_simulation` caught exceptions
    from their per-iteration work, so any unexpected bug (a stale
    `KeyError`, a DB error, etc.) silently froze the world clock/peer
    movement for the rest of the process's life with no visible signal.
    """

    @pytest.mark.anyio
    async def test_tick_loop_survives_a_bad_iteration(
        self, monkeypatch: pytest.MonkeyPatch, fast_sleep: None
    ) -> None:
        del fast_sleep
        calls = 0

        def flaky_tick_state(
            _state: WorldState, _tick_ms: int
        ) -> dict[str, object] | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                message = "boom"
                raise RuntimeError(message)
            return None

        monkeypatch.setattr("peerport.server.app.tick_state", flaky_tick_state)
        task = asyncio.ensure_future(
            _tick_loop(WorldState(), FakeBroadcaster(), tick_ms=100)  # type: ignore[arg-type]
        )
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert calls >= 2

    @pytest.mark.anyio
    async def test_simulation_tick_loop_survives_a_bad_iteration(
        self, fast_sleep: None
    ) -> None:
        del fast_sleep
        calls = 0

        class FlakySimulation:
            def tick(self, _tick_ms: int) -> list[dict[str, object]]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    message = "boom"
                    raise RuntimeError(message)
                return []

        fake_simulation: Simulation = FlakySimulation()  # type: ignore[assignment]
        fake_broadcaster: Broadcaster = FakeBroadcaster()  # type: ignore[assignment]
        task = asyncio.ensure_future(
            _tick_loop_simulation(fake_simulation, fake_broadcaster, tick_ms=100)
        )
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert calls >= 2
