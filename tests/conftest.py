"""Shared test fixtures."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def make_rng() -> Callable[[int], random.Random]:
    """Return a factory for seeded RNGs (deterministic sim tests)."""

    def _make(seed: int) -> random.Random:

        return random.Random(seed)  # noqa: S311

    return _make
