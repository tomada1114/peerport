"""Daily spend tracking and the soft/hard budget caps (requirements §4.9).

Soft cap (default $0.50/day) flips low-power mode: doubled activity
intervals and a reduced conversation turn limit. Hard cap (default
$2.00/day) refuses further LLM calls and signals a world pause. Days
roll over at UTC midnight, clearing both states.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import TYPE_CHECKING

from peerport.config import (
    DEFAULT_BUDGET_HARD_CAP_USD,
    DEFAULT_BUDGET_SOFT_CAP_USD,
)
from peerport.errors import BudgetExceededError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

BASE_ACTIVITY_BOUNDS = (60, 120)
LOW_POWER_ACTIVITY_BOUNDS = (120, 240)
BASE_TURN_LIMIT = 6
LOW_POWER_TURN_LIMIT = 4


class BudgetGuard:
    """Reads `usage_log` to enforce the daily soft and hard caps."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        soft_cap_usd: float = DEFAULT_BUDGET_SOFT_CAP_USD,
        hard_cap_usd: float = DEFAULT_BUDGET_HARD_CAP_USD,
        on_hard_cap: Callable[[], None] | None = None,
    ) -> None:
        """Configure caps and the pause signal.

        Args:
            conn: Open database connection (usage_log lives there).
            soft_cap_usd: Daily spend that enables low-power mode.
            hard_cap_usd: Daily spend that refuses further LLM calls.
            on_hard_cap: Called once per `check_hard_cap()` violation to
                signal the world to pause (wired to the sim by #27).
        """
        self._conn = conn
        self.soft_cap_usd = soft_cap_usd
        self.hard_cap_usd = hard_cap_usd
        self.on_hard_cap = on_hard_cap
        self.notice_active = False

    def today_spend(self) -> float:
        """Sum today's (UTC) estimated cost from `usage_log`."""
        midnight = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(est_cost_usd), 0) FROM usage_log WHERE ts_real >= ?",
            (int(midnight.timestamp()),),
        ).fetchone()
        return float(row[0])

    @property
    def low_power(self) -> bool:
        """Whether cumulative daily spend has reached the soft cap."""
        return self.today_spend() >= self.soft_cap_usd

    def activity_interval_bounds(self) -> tuple[int, int]:
        """Peer activity interval bounds in seconds, doubled in low power."""
        return LOW_POWER_ACTIVITY_BOUNDS if self.low_power else BASE_ACTIVITY_BOUNDS

    def conversation_turn_limit(self) -> int:
        """Peer conversation turn cap, reduced in low power."""
        return LOW_POWER_TURN_LIMIT if self.low_power else BASE_TURN_LIMIT

    def check_hard_cap(self) -> None:
        """Refuse further LLM dispatch once the hard cap is reached.

        Raises:
            BudgetExceededError: When today's spend has reached the hard
                cap; the pause signal fires and the Bridge notice flag is
                set before raising.
        """
        spend = self.today_spend()
        if spend >= self.hard_cap_usd:
            self.notice_active = True
            if self.on_hard_cap is not None:
                self.on_hard_cap()
            message = (
                f"daily hard cap reached: ${spend:.4f} >= ${self.hard_cap_usd:.2f}"
            )
            raise BudgetExceededError(message)
        self.notice_active = False
