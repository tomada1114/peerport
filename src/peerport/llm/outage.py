"""LLM/API outage tracking for the diegetic fog overlay (#27, requirements §5.2).

A single failed dispatch is already absorbed by `LLMClient`'s own
retry/backoff (`llm/client.py`) and must never flip the outage state on
its own - only `FAILURE_THRESHOLD` consecutive failed dispatches trip
it, so one transient blip never fogs the harbor. Recovery is immediate:
the very next successful dispatch clears it, matching REQ-004's
"no user action required" auto-recovery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

FAILURE_THRESHOLD = 2


class OutageTracker:
    """Consecutive-failure counter that flips a diegetic outage state on/off.

    Every LLM call site (`LLMClient._attempt_with_backoff`, `call_stream`)
    reports its outcome here; `on_change` fires only on an actual state
    transition, never on a repeat report of the same state.
    """

    def __init__(
        self, on_change: Callable[[bool, int | None], None] | None = None
    ) -> None:
        """Start inactive with a clean failure streak.

        Args:
            on_change: Called with `(active, status)` exactly when the
                outage state flips; `status` is the triggering HTTP
                status code when known, `None` on recovery.
        """
        self.on_change = on_change
        self.active = False
        self._consecutive_failures = 0

    def report_success(self) -> None:
        """Reset the failure streak; clear an active outage, if any."""
        self._consecutive_failures = 0
        if self.active:
            self.active = False
            if self.on_change is not None:
                self.on_change(False, None)

    def report_failure(self, status: int | None = None) -> None:
        """Count one failed dispatch; trip the outage at the threshold.

        Args:
            status: The HTTP status code of the failing call, when known
                (fed to the Bridge's `state.fog.detail` line).
        """
        self._consecutive_failures += 1
        if not self.active and self._consecutive_failures >= FAILURE_THRESHOLD:
            self.active = True
            if self.on_change is not None:
                self.on_change(True, status)
