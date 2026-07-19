"""Tests for `peerport.llm.budget` (daily spend, soft/hard caps)."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import pytest
from peerport.llm.budget import (
    BASE_ACTIVITY_BOUNDS,
    BASE_TURN_LIMIT,
    LOW_POWER_ACTIVITY_BOUNDS,
    LOW_POWER_TURN_LIMIT,
    BudgetGuard,
)

from peerport.db import UsageRecord, insert_usage, open_db
from peerport.errors import BudgetExceededError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


def spend(conn: sqlite3.Connection, cost: float, ts_real: int | None = None) -> None:
    insert_usage(
        conn,
        UsageRecord(
            model="gpt-5-nano",
            role="background",
            purpose="test",
            input_tokens=100,
            cached_tokens=0,
            output_tokens=10,
            est_cost_usd=cost,
            status="ok",
            ts_real=ts_real,
        ),
    )


class TestSoftCap:
    def test_below_soft_cap_is_not_low_power(self, conn: sqlite3.Connection) -> None:
        guard = BudgetGuard(conn)
        spend(conn, 0.499)
        assert guard.low_power is False

    def test_at_soft_cap_flips_low_power(self, conn: sqlite3.Connection) -> None:
        guard = BudgetGuard(conn)
        spend(conn, 0.50)
        assert guard.low_power is True

    def test_low_power_doubles_interval_and_reduces_turns(
        self, conn: sqlite3.Connection
    ) -> None:
        guard = BudgetGuard(conn)
        assert guard.activity_interval_bounds() == BASE_ACTIVITY_BOUNDS == (60, 120)
        assert guard.conversation_turn_limit() == BASE_TURN_LIMIT == 6
        spend(conn, 0.75)
        assert guard.activity_interval_bounds() == LOW_POWER_ACTIVITY_BOUNDS
        assert guard.activity_interval_bounds() == (120, 240)
        assert guard.conversation_turn_limit() == LOW_POWER_TURN_LIMIT == 4


class TestHardCap:
    def test_at_hard_cap_check_raises_and_signals(
        self, conn: sqlite3.Connection
    ) -> None:
        pauses: list[str] = []
        guard = BudgetGuard(conn, on_hard_cap=lambda: pauses.append("pause"))
        spend(conn, 2.00)
        with pytest.raises(BudgetExceededError):
            guard.check_hard_cap()
        assert pauses == ["pause"]
        assert guard.notice_active is True

    def test_below_hard_cap_check_passes(self, conn: sqlite3.Connection) -> None:
        guard = BudgetGuard(conn)
        spend(conn, 1.998)
        guard.check_hard_cap()
        assert guard.notice_active is False

    def test_caps_configurable(self, conn: sqlite3.Connection) -> None:
        guard = BudgetGuard(conn, soft_cap_usd=0.10, hard_cap_usd=0.20)
        spend(conn, 0.15)
        assert guard.low_power is True
        spend(conn, 0.05)
        with pytest.raises(BudgetExceededError):
            guard.check_hard_cap()


class TestUtcRollover:
    def test_yesterday_spend_not_counted_today(self, conn: sqlite3.Connection) -> None:
        guard = BudgetGuard(conn)
        now = dt.datetime.now(tz=dt.UTC)
        yesterday = int((now - dt.timedelta(days=1)).timestamp())
        spend(conn, 1.90, ts_real=yesterday)
        assert guard.today_spend() == 0.0
        assert guard.low_power is False
        guard.check_hard_cap()

    def test_today_spend_sums_only_today(self, conn: sqlite3.Connection) -> None:
        guard = BudgetGuard(conn)
        now = dt.datetime.now(tz=dt.UTC)
        spend(conn, 0.30, ts_real=int((now - dt.timedelta(days=2)).timestamp()))
        spend(conn, 0.10)
        spend(conn, 0.05)
        assert guard.today_spend() == pytest.approx(0.15)
