from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from sunset_sentinel_api.adapters.http_client import HttpLifecycleClient
from sunset_sentinel_api.adapters.sqlite_repository import SQLiteRepository
from sunset_sentinel_api.clock import FrozenClock
from sunset_sentinel_api.domain.enums import HeaderMode, SignalCompliance
from sunset_sentinel_api.services.http_scan import HttpScanTarget, scan_http_target

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _client(
    handler: httpx.MockTransport,
    *,
    allowed_hosts: tuple[str, ...] = ("api.example.com",),
) -> HttpLifecycleClient:
    return HttpLifecycleClient(
        allowed_hosts=allowed_hosts,
        clock=FrozenClock(NOW),
        transport=handler,
        minimum_origin_interval_seconds=0,
    )


def test_http_scan_parses_and_persists_redacted_lifecycle_metadata() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={
                "Deprecation": "@1782863999",
                "Sunset": "Wed, 30 Sep 2026 23:59:59 GMT",
                "Link": '</migration/orders?account=private>; rel="deprecation"',
                "Cache-Control": "max-age=60",
            },
            request=request,
        )
    )
    with SQLiteRepository(":memory:", clock=FrozenClock(NOW)) as repository:
        with _client(transport) as client:
            outcome = scan_http_target(
                client=client,
                repository=repository,
                target=HttpScanTarget(
                    target_id="acme",
                    url="https://api.example.com/v1/orders?token=secret",
                ),
                observed_at=NOW,
            )

        records = repository.list_signal_records()

    assert outcome.persisted is True
    assert len(records) == 1
    signal = records[0].signal
    assert signal.compliance is SignalCompliance.RFC
    assert signal.source_ref == "https://api.example.com/v1/orders?token=REDACTED"
    assert signal.documentation_url is not None
    assert "private" not in signal.documentation_url
    assert signal.documentation_url.endswith("account=REDACTED")
    assert signal.endpoint is not None
    assert signal.endpoint.path == "/v1/orders"


def test_http_scan_strict_mode_rejects_legacy_boolean_without_persisting() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Deprecation": "true"},
            request=request,
        )
    )
    with SQLiteRepository(":memory:", clock=FrozenClock(NOW)) as repository:
        with _client(transport) as client:
            outcome = scan_http_target(
                client=client,
                repository=repository,
                target=HttpScanTarget(
                    target_id="acme",
                    url="https://api.example.com/v1/search",
                ),
                observed_at=NOW,
                mode=HeaderMode.STRICT,
            )
        signals = repository.list_signals()

    assert outcome.fetch.ok is True
    assert outcome.persisted is False
    assert signals == ()
    assert any(
        diagnostic.code == "invalid_deprecation_header" for diagnostic in outcome.diagnostics
    )


def test_http_scan_withdraws_prior_signal_when_both_headers_disappear() -> None:
    responses = iter(
        (
            {
                "Deprecation": "@1782863999",
                "Sunset": "Wed, 30 Sep 2026 23:59:59 GMT",
                "Cache-Control": "no-store",
            },
            {"Cache-Control": "no-store"},
        )
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers=next(responses),
            request=request,
        )
    )
    target = HttpScanTarget(
        target_id="acme",
        url="https://api.example.com/v1/orders",
    )
    later = NOW + timedelta(hours=1)

    with SQLiteRepository(":memory:", clock=FrozenClock(NOW)) as repository:
        with _client(transport) as client:
            discovered = scan_http_target(
                client=client,
                repository=repository,
                target=target,
                observed_at=NOW,
            )
            withdrawn = scan_http_target(
                client=client,
                repository=repository,
                target=target,
                observed_at=later,
            )
        changes = repository.list_changes()
        active = repository.list_signals(active_only=True)

    assert discovered.persisted is True
    assert withdrawn.persisted is True
    assert withdrawn.parsed_signal is not None
    assert withdrawn.parsed_signal.active is False
    assert [change.change_type for change in changes] == ["discovered", "withdrawn"]
    assert active == ()


def test_http_scan_blocked_host_never_invokes_transport_or_writes() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, request=request)

    with SQLiteRepository(":memory:", clock=FrozenClock(NOW)) as repository:
        with _client(httpx.MockTransport(handler)) as client:
            outcome = scan_http_target(
                client=client,
                repository=repository,
                target=HttpScanTarget(
                    target_id="acme",
                    url="https://blocked.example/v1/orders",
                ),
                observed_at=NOW,
            )
        signals = repository.list_signals()

    assert outcome.fetch.error_code == "host_not_allowed"
    assert outcome.persisted is False
    assert called is False
    assert signals == ()
