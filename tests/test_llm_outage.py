"""Tests for `peerport.llm.outage` (the diegetic fog state machine, #27)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from peerport.llm.outage import FAILURE_THRESHOLD, OutageTracker

if TYPE_CHECKING:
    from collections.abc import Callable


def _recorder() -> tuple[
    list[tuple[bool, int | None]], Callable[[bool, int | None], None]
]:
    """A `changes` list plus an `on_change` callback that appends to it."""
    changes: list[tuple[bool, int | None]] = []

    def on_change(active: bool, status: int | None) -> None:
        changes.append((active, status))

    return changes, on_change


class TestFailureThreshold:
    """A single failed dispatch must never flip the tracker active."""

    def test_starts_inactive(self) -> None:
        tracker = OutageTracker()

        assert tracker.active is False

    def test_single_failure_does_not_activate(self) -> None:
        changes, on_change = _recorder()
        tracker = OutageTracker(on_change=on_change)

        tracker.report_failure(status=503)

        assert tracker.active is False
        assert changes == []

    def test_second_consecutive_failure_activates_with_status(self) -> None:
        changes, on_change = _recorder()
        tracker = OutageTracker(on_change=on_change)

        tracker.report_failure(status=503)
        tracker.report_failure(status=503)

        assert tracker.active is True
        assert changes == [(True, 503)]

    def test_threshold_is_two(self) -> None:
        assert FAILURE_THRESHOLD == 2

    def test_third_consecutive_failure_does_not_refire_on_change(self) -> None:
        changes, on_change = _recorder()
        tracker = OutageTracker(on_change=on_change)

        tracker.report_failure(status=503)
        tracker.report_failure(status=503)
        tracker.report_failure(status=500)

        assert tracker.active is True
        assert changes == [(True, 503)]  # no second firing for the 3rd failure

    def test_success_between_failures_resets_the_streak(self) -> None:
        changes, on_change = _recorder()
        tracker = OutageTracker(on_change=on_change)

        tracker.report_failure(status=503)
        tracker.report_success()
        tracker.report_failure(status=503)

        assert tracker.active is False
        assert changes == []


class TestRecovery:
    """Any single success clears an active outage (REQ-004: no user action)."""

    def test_success_after_active_clears_it(self) -> None:
        changes, on_change = _recorder()
        tracker = OutageTracker(on_change=on_change)
        tracker.report_failure(status=503)
        tracker.report_failure(status=503)

        tracker.report_success()

        assert tracker.active is False
        assert changes == [(True, 503), (False, None)]

    def test_success_while_already_inactive_does_not_fire_on_change(self) -> None:
        changes, on_change = _recorder()
        tracker = OutageTracker(on_change=on_change)

        tracker.report_success()

        assert tracker.active is False
        assert changes == []


class TestNoOnChangeCallback:
    """`on_change` is optional; the tracker still tracks state without one."""

    def test_activation_without_callback_does_not_raise(self) -> None:
        tracker = OutageTracker()

        tracker.report_failure()
        tracker.report_failure()

        assert tracker.active is True

    def test_recovery_without_callback_does_not_raise(self) -> None:
        tracker = OutageTracker()
        tracker.report_failure()
        tracker.report_failure()

        tracker.report_success()

        assert tracker.active is False
