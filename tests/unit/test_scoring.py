from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sunset_sentinel_api.domain import (
    Band,
    Consumer,
    ConsumerDependency,
    Criticality,
    EndpointRef,
    blast_radius_band,
    blast_radius_score,
    priority_score,
    score_lifecycle,
    urgency_score,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=-1), 100),
        (timedelta(0), 100),
        (timedelta(days=7), 95),
        (timedelta(days=7, seconds=1), 85),
        (timedelta(days=30), 85),
        (timedelta(days=90), 70),
        (timedelta(days=180), 55),
        (timedelta(days=365), 35),
        (timedelta(days=365, seconds=1), 15),
    ],
)
def test_sunset_urgency_boundaries(delta: timedelta, expected: int) -> None:
    assert urgency_score(now=NOW, sunset_at=NOW + delta) == expected


def test_urgency_is_monotone_as_sunset_approaches() -> None:
    scores = [
        urgency_score(now=NOW, sunset_at=NOW + timedelta(days=days))
        for days in (500, 365, 180, 90, 30, 7, 0, -1)
    ]

    assert scores == sorted(scores)


def test_deprecation_unknown_date_and_conflict_scores() -> None:
    assert urgency_score(now=NOW, deprecated=True) == 60
    assert urgency_score(now=NOW, deprecation_at=NOW) == 75
    assert (
        urgency_score(
            now=NOW,
            deprecation_at=NOW + timedelta(days=90),
            sunset_at=NOW + timedelta(days=30),
        )
        == 95
    )


def test_blast_radius_deduplicates_entities_and_edges() -> None:
    endpoint = EndpointRef(target_id="payments", method="GET", path="/v1/orders")
    low = Consumer(id="worker", name="Worker", criticality=Criticality.LOW)
    critical = Consumer(id="checkout", name="Checkout", criticality=Criticality.CRITICAL)
    edge = ConsumerDependency(consumer_id="worker", endpoint_key=endpoint.key)
    critical_edge = ConsumerDependency(consumer_id="checkout", endpoint_key=endpoint.key)

    score = blast_radius_score(
        endpoints=[endpoint, endpoint],
        consumers=[low, low, critical],
        dependencies=[edge, edge, critical_edge],
    )

    assert score == 31  # endpoints 5 + consumers 22 + edges 4
    assert blast_radius_band(score) is Band.MEDIUM


def test_blast_radius_ignores_unlisted_consumers_and_unaffected_edges() -> None:
    endpoint = EndpointRef(target_id="payments", method="GET", path="/v1/orders")
    other = EndpointRef(target_id="payments", method="POST", path="/v1/refunds")
    consumer = Consumer(id="worker", name="Worker")

    score = blast_radius_score(
        endpoints=[endpoint],
        consumers=[consumer],
        dependencies=[
            ConsumerDependency(consumer_id="worker", endpoint_key=other.key),
            ConsumerDependency(consumer_id="unknown", endpoint_key=endpoint.key),
        ],
    )

    assert score == 10  # endpoint 5 + medium consumer 5


def test_service_scope_and_caps_are_deterministic() -> None:
    consumers = [
        Consumer(id=f"consumer-{index}", name=f"Consumer {index}", criticality="critical")
        for index in range(10)
    ]
    endpoints = [
        EndpointRef(target_id="payments", method="GET", path=f"/v1/items/{index}")
        for index in range(10)
    ]
    dependencies = [
        ConsumerDependency(consumer_id=consumer.id, endpoint_key=endpoint.key)
        for consumer, endpoint in zip(consumers, endpoints, strict=True)
    ]

    assert (
        blast_radius_score(
            endpoints=endpoints,
            consumers=consumers,
            dependencies=dependencies,
            service_scope=True,
        )
        == 100
    )


def test_priority_never_reduces_urgency_and_impact_can_lift_it() -> None:
    assert priority_score(urgency=100, blast_radius=0) == 100
    assert priority_score(urgency=15, blast_radius=100) == 58


def test_combined_score_card_has_consistent_bands() -> None:
    card = score_lifecycle(now=NOW, deprecated=True)

    assert card.urgency == 60
    assert card.urgency_band is Band.MEDIUM
    assert card.blast_radius == 0
    assert card.priority == 60
    assert card.priority_band is Band.MEDIUM


@pytest.mark.parametrize("score", [-1, 101])
def test_priority_rejects_out_of_range_inputs(score: int) -> None:
    with pytest.raises(ValueError):
        priority_score(urgency=score, blast_radius=0)
