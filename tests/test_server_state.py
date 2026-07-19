"""Tests for peerport.server.state (pure tick/diff/broadcast logic)."""

from __future__ import annotations

import asyncio

from peerport.server.state import (
    Broadcaster,
    PeerPosition,
    WorldState,
    snapshot,
    tick_state,
)


class TestSnapshot:
    def test_shape_with_no_peers(self) -> None:
        state = WorldState()

        snap = snapshot(state)

        assert snap["t"] == "snapshot"
        assert snap["clock"] == {"world_seconds": 0}
        assert snap["peers"] == {}
        assert snap["events"] == []

    def test_includes_all_current_peers(self) -> None:
        state = WorldState(peers={"beacon": PeerPosition(pos_x=1, pos_y=2)})

        snap = snapshot(state)

        assert snap["peers"] == {"beacon": {"pos_x": 1, "pos_y": 2}}


class TestTickState:
    def test_returns_none_when_nothing_changed(self) -> None:
        state = WorldState(peers={"beacon": PeerPosition(pos_x=0, pos_y=0)})

        diff = tick_state(state, tick_ms=100)

        assert diff is None

    def test_returns_clock_diff_on_second_rollover(self) -> None:
        state = WorldState()

        diff = tick_state(state, tick_ms=1000)

        assert diff == {"t": "diff", "clock": {"world_seconds": 1}}

    def test_no_clock_diff_for_a_sub_second_tick(self) -> None:
        state = WorldState()

        diff = tick_state(state, tick_ms=100)

        assert diff is None

    def test_ten_sub_second_ticks_roll_over_exactly_once(self) -> None:
        state = WorldState()

        diffs = [tick_state(state, tick_ms=100) for _ in range(10)]

        assert diffs[:9] == [None] * 9
        assert diffs[9] == {"t": "diff", "clock": {"world_seconds": 1}}

    def test_returns_diff_for_a_moved_peer_only(self) -> None:
        state = WorldState(
            peers={
                "beacon": PeerPosition(pos_x=0, pos_y=0),
                "tug": PeerPosition(pos_x=5, pos_y=5),
            }
        )
        state.peers["beacon"] = PeerPosition(pos_x=1, pos_y=0)

        diff = tick_state(state, tick_ms=100)

        assert diff == {"t": "diff", "peers": {"beacon": {"pos_x": 1, "pos_y": 0}}}

    def test_unmoved_peer_produces_no_diff_after_initial_state(self) -> None:
        state = WorldState(peers={"beacon": PeerPosition(pos_x=0, pos_y=0)})

        first = tick_state(state, tick_ms=100)
        second = tick_state(state, tick_ms=100)

        assert first is None
        assert second is None

    def test_world_advances_across_many_ticks_without_asyncio(self) -> None:
        """Verify pure tick stepping.

        Per architecture.md §6, tick logic must be testable as a pure
        function of state, with no real asyncio timing involved.
        """
        state = WorldState()

        for _ in range(50):
            tick_state(state, tick_ms=100)

        assert int(state.world_seconds) == 5


class TestBroadcaster:
    def test_subscriber_receives_published_message(self) -> None:
        async def scenario() -> dict[str, object]:
            broadcaster = Broadcaster()
            queue = broadcaster.subscribe()
            await broadcaster.publish({"t": "diff"})
            return await asyncio.wait_for(queue.get(), timeout=1)

        result = asyncio.run(scenario())

        assert result == {"t": "diff"}

    def test_unsubscribed_queue_receives_nothing_further(self) -> None:
        async def scenario() -> asyncio.Queue[dict[str, object]]:
            broadcaster = Broadcaster()
            queue = broadcaster.subscribe()
            broadcaster.unsubscribe(queue)
            await broadcaster.publish({"t": "diff"})
            return queue

        queue = asyncio.run(scenario())

        assert queue.empty()

    def test_all_subscribers_receive_the_same_message(self) -> None:
        async def scenario() -> tuple[dict[str, object], dict[str, object]]:
            broadcaster = Broadcaster()
            queue_a = broadcaster.subscribe()
            queue_b = broadcaster.subscribe()
            await broadcaster.publish({"t": "diff", "clock": {"world_seconds": 1}})
            return await queue_a.get(), await queue_b.get()

        message_a, message_b = asyncio.run(scenario())

        assert message_a == message_b == {"t": "diff", "clock": {"world_seconds": 1}}
