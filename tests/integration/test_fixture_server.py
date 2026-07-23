from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from sunset_sentinel_api.adapters.fixture_server import create_fixture_app
from sunset_sentinel_api.domain import HeaderMode, LifecycleState, parse_lifecycle_headers
from sunset_sentinel_api.domain.lifecycle import determine_lifecycle_state

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def test_fixture_server_exercises_three_real_lifecycle_scenarios() -> None:
    with TestClient(create_fixture_app()) as client:
        orders = client.get("/v1/orders")
        search = client.get("/v1/search")
        conflict = client.get("/v1/conflict")

    orders_headers = parse_lifecycle_headers(
        orders.headers,
        now=NOW,
        response_url=str(orders.request.url),
    )
    search_headers = parse_lifecycle_headers(
        search.headers,
        now=NOW,
        mode=HeaderMode.COMPAT,
        response_url=str(search.request.url),
    )
    conflict_headers = parse_lifecycle_headers(
        conflict.headers,
        now=NOW,
        response_url=str(conflict.request.url),
    )

    assert orders.status_code == 200
    assert orders_headers.deprecation.value == datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
    assert orders_headers.documentation_urls == ("http://testserver/migration/orders",)

    assert search_headers.deprecation.deprecated is True
    assert search_headers.deprecation.diagnostics[0].code == "legacy_deprecation_boolean"

    assert (
        determine_lifecycle_state(
            now=NOW,
            deprecation_at=conflict_headers.deprecation.value,
            sunset_at=conflict_headers.sunset.value,
        )
        is LifecycleState.CONFLICTED
    )


def test_fixture_server_supports_conditional_cache_validation() -> None:
    with TestClient(create_fixture_app()) as client:
        first = client.get("/v1/orders")
        second = client.get("/v1/orders", headers={"If-None-Match": first.headers["etag"]})

    assert first.status_code == 200
    assert second.status_code == 304
    assert second.headers["etag"] == '"orders-lifecycle-v1"'
