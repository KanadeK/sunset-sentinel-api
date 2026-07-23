from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sunset_sentinel_api.domain import (
    Band,
    EndpointRef,
    LifecycleSignal,
    ScopeKind,
    ScoreCard,
    SignalSource,
    determine_lifecycle_state,
)
from sunset_sentinel_api.domain.enums import LifecycleState


def test_endpoint_is_normalized_frozen_and_forbids_extra_fields() -> None:
    endpoint = EndpointRef(target_id="payments", method="get", path="/v1/orders")

    assert endpoint.method == "GET"
    assert endpoint.key == "payments\nGET\n/v1/orders"
    with pytest.raises(ValidationError):
        endpoint.__setattr__("method", "POST")
    with pytest.raises(ValidationError):
        EndpointRef.model_validate(
            {
                "target_id": "payments",
                "method": "GET",
                "path": "/v1/orders",
                "unexpected": True,
            }
        )


@pytest.mark.parametrize("path", ["v1/orders", "/v1/orders?secret=value", "/v1#fragment"])
def test_endpoint_rejects_noncanonical_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        EndpointRef(target_id="payments", method="GET", path=path)


def test_signal_normalizes_aware_datetimes_to_utc_seconds() -> None:
    endpoint = EndpointRef(target_id="payments", method="GET", path="/v1/orders")
    signal = LifecycleSignal(
        signal_key="scheduled-retirement",
        target_id="payments",
        source=SignalSource.MANUAL,
        source_ref="feed.yml#scheduled-retirement",
        scope=ScopeKind.ENDPOINT,
        endpoint=endpoint,
        deprecation_at=datetime(
            2030,
            1,
            1,
            8,
            0,
            0,
            900,
            tzinfo=timezone(timedelta(hours=8)),
        ),
        observed_at=datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=8))),
        raw_sha256="0" * 64,
    )

    assert signal.deprecation_at == datetime(2030, 1, 1, tzinfo=UTC)
    assert signal.observed_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_signal_rejects_naive_time_and_scope_mismatch() -> None:
    endpoint = EndpointRef(target_id="payments", method="GET", path="/v1/orders")
    common = {
        "signal_key": "scheduled-retirement",
        "target_id": "payments",
        "source": SignalSource.MANUAL,
        "source_ref": "feed.yml#scheduled-retirement",
        "scope": ScopeKind.ENDPOINT,
        "endpoint": endpoint,
        "raw_sha256": "0" * 64,
    }
    with pytest.raises(ValidationError, match="timezone"):
        LifecycleSignal(
            **common,
            deprecation_at=datetime(2030, 1, 1),
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    with pytest.raises(ValidationError, match="endpoint"):
        LifecycleSignal(
            **(common | {"scope": ScopeKind.SERVICE}),
            deprecation_at=datetime(2030, 1, 1, tzinfo=UTC),
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_score_card_forbids_out_of_range_scores() -> None:
    with pytest.raises(ValidationError):
        ScoreCard(
            urgency=101,
            urgency_band=Band.CRITICAL,
            blast_radius=0,
            blast_radius_band=Band.NONE,
            priority=100,
            priority_band=Band.CRITICAL,
        )


def test_lifecycle_state_ordering_and_conflict() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert (
        determine_lifecycle_state(now=now, sunset_at=now - timedelta(seconds=1))
        is LifecycleState.SUNSET_OVERDUE
    )
    assert (
        determine_lifecycle_state(
            now=now,
            deprecation_at=now + timedelta(days=30),
            sunset_at=now + timedelta(days=10),
        )
        is LifecycleState.CONFLICTED
    )
    assert (
        determine_lifecycle_state(now=now, deprecated=True)
        is LifecycleState.DEPRECATED_DATE_UNKNOWN
    )
    assert determine_lifecycle_state(now=now, active=False) is LifecycleState.WITHDRAWN


def test_state_rejects_naive_clock() -> None:
    with pytest.raises(ValueError, match="timezone"):
        determine_lifecycle_state(now=datetime(2026, 1, 1))
