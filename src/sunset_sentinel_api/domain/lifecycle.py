"""Pure lifecycle state derivation."""

from __future__ import annotations

from datetime import datetime

from sunset_sentinel_api.domain.enums import LifecycleState
from sunset_sentinel_api.domain.models import as_utc


def determine_lifecycle_state(
    *,
    now: datetime,
    deprecated: bool = False,
    deprecation_at: datetime | None = None,
    sunset_at: datetime | None = None,
    active: bool = True,
    conflict: bool = False,
) -> LifecycleState:
    """Derive state from normalized evidence with no I/O or system clock access."""

    normalized_now = as_utc(now, field_name="now")
    normalized_deprecation = (
        None if deprecation_at is None else as_utc(deprecation_at, field_name="deprecation_at")
    )
    normalized_sunset = None if sunset_at is None else as_utc(sunset_at, field_name="sunset_at")

    if not active:
        return LifecycleState.WITHDRAWN
    if conflict or (
        normalized_deprecation is not None
        and normalized_sunset is not None
        and normalized_sunset < normalized_deprecation
    ):
        return LifecycleState.CONFLICTED
    if normalized_sunset is not None and normalized_sunset <= normalized_now:
        return LifecycleState.SUNSET_OVERDUE
    if normalized_deprecation is not None and normalized_deprecation <= normalized_now:
        return LifecycleState.DEPRECATED
    if normalized_deprecation is not None:
        return LifecycleState.DEPRECATION_SCHEDULED
    if normalized_sunset is not None:
        return LifecycleState.SUNSET_SCHEDULED
    if deprecated:
        return LifecycleState.DEPRECATED_DATE_UNKNOWN
    return LifecycleState.ACTIVE
