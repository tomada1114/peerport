"""Tests for `peerport.world.clock` (world clock, day bands)."""

from __future__ import annotations

import pytest

from peerport.world.clock import BANDS, WorldClock

DAY_SECONDS = 120 * 60  # default day length
QUARTER = DAY_SECONDS // 4


class TestBands:
    def test_band_order_constant(self) -> None:
        assert BANDS == ("morning", "day", "dusk", "night")

    @pytest.mark.parametrize(
        ("seconds", "band"),
        [
            pytest.param(0, "morning", id="day-start"),
            pytest.param(QUARTER - 1, "morning", id="morning-end"),
            pytest.param(QUARTER, "day", id="day-band-start"),
            pytest.param(2 * QUARTER, "dusk", id="dusk-start"),
            pytest.param(3 * QUARTER, "night", id="night-start"),
            pytest.param(DAY_SECONDS - 1, "night", id="night-end"),
            pytest.param(DAY_SECONDS, "morning", id="wraps-to-morning"),
        ],
    )
    def test_band_at_default_day_length(self, seconds: int, band: str) -> None:
        assert WorldClock().band(seconds) == band

    def test_band_honors_configured_day_length(self) -> None:
        clock = WorldClock(day_length_real_minutes=60)
        assert clock.band(0) == "morning"
        assert clock.band(900) == "day"
        assert clock.band(1800) == "dusk"
        assert clock.band(2700) == "night"

    def test_bands_cycle_in_order_with_no_skips(self) -> None:
        clock = WorldClock(day_length_real_minutes=4)
        transitions = []
        previous = clock.band(0)
        for second in range(1, 2 * 4 * 60 + 1):
            current = clock.band(second)
            if current != previous:
                transitions.append((previous, current))
                previous = current
        expected_cycle = [
            ("morning", "day"),
            ("day", "dusk"),
            ("dusk", "night"),
            ("night", "morning"),
        ]
        assert transitions == expected_cycle * 2

    def test_dusk_to_night_boundary_at_90_minutes(self) -> None:
        clock = WorldClock()
        assert clock.band(90 * 60 - 1) == "dusk"
        assert clock.band(90 * 60) == "night"


class TestDay:
    def test_first_day_is_day_one(self) -> None:
        assert WorldClock().day(0) == 1

    def test_day_increments_at_day_length(self) -> None:
        clock = WorldClock()
        assert clock.day(DAY_SECONDS - 1) == 1
        assert clock.day(DAY_SECONDS) == 2

    def test_invalid_day_length_rejected(self) -> None:
        with pytest.raises(ValueError, match="day_length_real_minutes"):
            WorldClock(day_length_real_minutes=0)
