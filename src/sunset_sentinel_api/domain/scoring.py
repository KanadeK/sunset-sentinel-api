"""Deterministic urgency, impact, and priority scoring."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from sunset_sentinel_api.domain.enums import Band, Criticality
from sunset_sentinel_api.domain.models import (
    Consumer,
    ConsumerDependency,
    EndpointRef,
    ScoreCard,
    as_utc,
)

_SUNSET_THRESHOLDS: tuple[tuple[timedelta | None, int], ...] = (
    (timedelta(0), 100),
    (timedelta(days=7), 95),
    (timedelta(days=30), 85),
    (timedelta(days=90), 70),
    (timedelta(days=180), 55),
    (timedelta(days=365), 35),
    (None, 15),
)
_DEPRECATION_THRESHOLDS: tuple[tuple[timedelta | None, int], ...] = (
    (timedelta(0), 75),
    (timedelta(days=7), 70),
    (timedelta(days=30), 60),
    (timedelta(days=90), 45),
    (timedelta(days=180), 30),
    (timedelta(days=365), 20),
    (None, 10),
)
_CRITICALITY_WEIGHT = {
    Criticality.LOW: 2,
    Criticality.MEDIUM: 5,
    Criticality.HIGH: 10,
    Criticality.CRITICAL: 20,
}


def _deadline_score(
    *,
    deadline: datetime,
    now: datetime,
    thresholds: tuple[tuple[timedelta | None, int], ...],
) -> int:
    delta = deadline - now
    for limit, score in thresholds:
        if limit is None or delta <= limit:
            return score
    raise AssertionError("deadline score table must end with an unbounded threshold")


def urgency_score(
    *,
    now: datetime,
    deprecated: bool = False,
    deprecation_at: datetime | None = None,
    sunset_at: datetime | None = None,
    conflict: bool = False,
) -> int:
    """Calculate a monotone time-risk score from zero to one hundred."""

    normalized_now = as_utc(now, field_name="now")
    normalized_deprecation = (
        None if deprecation_at is None else as_utc(deprecation_at, field_name="deprecation_at")
    )
    normalized_sunset = None if sunset_at is None else as_utc(sunset_at, field_name="sunset_at")

    scores = [0]
    if normalized_deprecation is not None:
        scores.append(
            _deadline_score(
                deadline=normalized_deprecation,
                now=normalized_now,
                thresholds=_DEPRECATION_THRESHOLDS,
            )
        )
    elif deprecated:
        scores.append(60)

    if normalized_sunset is not None:
        scores.append(
            _deadline_score(
                deadline=normalized_sunset,
                now=normalized_now,
                thresholds=_SUNSET_THRESHOLDS,
            )
        )

    standards_conflict = (
        normalized_deprecation is not None
        and normalized_sunset is not None
        and normalized_sunset < normalized_deprecation
    )
    score = max(scores)
    return max(score, 95) if conflict or standards_conflict else score


def urgency_band(score: int) -> Band:
    """Map urgency and combined-priority scores to stable bands."""

    _validate_score(score)
    if score == 0:
        return Band.NONE
    if score < 40:
        return Band.LOW
    if score < 70:
        return Band.MEDIUM
    if score < 90:
        return Band.HIGH
    return Band.CRITICAL


def blast_radius_score(
    *,
    endpoints: Iterable[EndpointRef],
    consumers: Iterable[Consumer],
    dependencies: Iterable[ConsumerDependency],
    service_scope: bool = False,
) -> int:
    """Score unique affected endpoints, consumers, and dependency edges."""

    endpoint_keys = {endpoint.key for endpoint in endpoints}
    consumer_weights: dict[str, int] = {}
    for consumer in consumers:
        weight = _CRITICALITY_WEIGHT[consumer.criticality]
        consumer_weights[consumer.id] = max(consumer_weights.get(consumer.id, 0), weight)

    dependency_edges: set[tuple[str, str]] = set()
    for dependency in dependencies:
        if dependency.consumer_id not in consumer_weights:
            continue
        if endpoint_keys and dependency.endpoint_key not in endpoint_keys:
            continue
        if not endpoint_keys and not service_scope:
            continue
        dependency_edges.add((dependency.consumer_id, dependency.endpoint_key))

    endpoint_points = min(25, 5 * len(endpoint_keys))
    consumer_points = min(50, sum(consumer_weights.values()))
    dependency_points = min(15, 2 * len(dependency_edges))
    scope_points = 10 if service_scope else 0
    return min(100, endpoint_points + consumer_points + dependency_points + scope_points)


def blast_radius_band(score: int) -> Band:
    """Map blast-radius scores to their impact-specific bands."""

    _validate_score(score)
    if score == 0:
        return Band.NONE
    if score < 25:
        return Band.LOW
    if score < 50:
        return Band.MEDIUM
    if score < 75:
        return Band.HIGH
    return Band.CRITICAL


def priority_score(*, urgency: int, blast_radius: int) -> int:
    """Lift urgency by broad impact without ever reducing time risk."""

    _validate_score(urgency)
    _validate_score(blast_radius)
    return max(urgency, (urgency + blast_radius + 1) // 2)


def score_lifecycle(
    *,
    now: datetime,
    deprecated: bool = False,
    deprecation_at: datetime | None = None,
    sunset_at: datetime | None = None,
    conflict: bool = False,
    endpoints: Iterable[EndpointRef] = (),
    consumers: Iterable[Consumer] = (),
    dependencies: Iterable[ConsumerDependency] = (),
    service_scope: bool = False,
) -> ScoreCard:
    """Return all three scores and bands in one immutable model."""

    urgency = urgency_score(
        now=now,
        deprecated=deprecated,
        deprecation_at=deprecation_at,
        sunset_at=sunset_at,
        conflict=conflict,
    )
    blast_radius = blast_radius_score(
        endpoints=endpoints,
        consumers=consumers,
        dependencies=dependencies,
        service_scope=service_scope,
    )
    priority = priority_score(urgency=urgency, blast_radius=blast_radius)
    return ScoreCard(
        urgency=urgency,
        urgency_band=urgency_band(urgency),
        blast_radius=blast_radius,
        blast_radius_band=blast_radius_band(blast_radius),
        priority=priority,
        priority_band=urgency_band(priority),
    )


def _validate_score(score: int) -> None:
    if not 0 <= score <= 100:
        raise ValueError("score must be between 0 and 100")
