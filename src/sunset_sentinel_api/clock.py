"""Injectable UTC clocks for deterministic lifecycle calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sunset_sentinel_api.domain.models import as_utc


class Clock(Protocol):
    """Source of current UTC time."""

    def now(self) -> datetime:
        """Return an aware current time."""


class SystemClock:
    """Production clock backed by the operating system."""

    def now(self) -> datetime:
        """Return the current time in UTC at second precision."""

        return datetime.now(UTC).replace(microsecond=0)


@dataclass(frozen=True, slots=True)
class FrozenClock:
    """Deterministic clock used by tests and reproducible demos."""

    instant: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "instant", as_utc(self.instant, field_name="instant"))

    def now(self) -> datetime:
        """Return the configured fixed instant."""

        return self.instant
