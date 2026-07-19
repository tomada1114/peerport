"""World clock: independent world time, day numbering, and light bands.

Per requirements.md §4.1 the world clock advances 1 world second per real
second only while the server runs; a world day spans
`world.day_length_real_minutes` (default 120) split into four equal light
bands.
"""

from __future__ import annotations

from dataclasses import dataclass

BANDS = ("morning", "day", "dusk", "night")

DEFAULT_DAY_LENGTH_REAL_MINUTES = 120


@dataclass(frozen=True, slots=True)
class WorldClock:
    """Maps absolute world seconds to day number and light band."""

    day_length_real_minutes: int = DEFAULT_DAY_LENGTH_REAL_MINUTES

    def __post_init__(self) -> None:
        """Validate the configured day length.

        Raises:
            ValueError: If `day_length_real_minutes` is not positive.
        """
        if self.day_length_real_minutes < 1:
            message = (
                f"day_length_real_minutes must be >= 1, "
                f"got {self.day_length_real_minutes}"
            )
            raise ValueError(message)

    @property
    def day_seconds(self) -> int:
        """Length of one world day in world seconds."""
        return self.day_length_real_minutes * 60

    def band(self, world_seconds: int) -> str:
        """Return the light band (morning/day/dusk/night) at *world_seconds*."""
        elapsed = world_seconds % self.day_seconds
        index = min(elapsed * len(BANDS) // self.day_seconds, len(BANDS) - 1)
        return BANDS[index]

    def day(self, world_seconds: int) -> int:
        """Return the 1-based world day number at *world_seconds*."""
        return world_seconds // self.day_seconds + 1
