from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from sunset_sentinel_api.adapters.http_client import (
    FetchStatus,
    HttpLifecycleClient,
    InMemoryCache,
    redact_query_values,
)


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_client(
    handler: httpx.MockTransport,
    clock: FrozenClock,
    *,
    minimum_origin_interval_seconds: int = 60,
) -> HttpLifecycleClient:
    return HttpLifecycleClient(
        allowed_hosts=["api.example.test"],
        clock=clock,
        transport=handler,
        minimum_origin_interval_seconds=minimum_origin_interval_seconds,
    )


def test_success_retains_only_lifecycle_headers_and_redacts_query() -> None:
    seen_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(
            200,
            headers=[
                ("Sunset", "Wed, 11 Nov 2026 11:11:11 GMT"),
                ("Deprecation", "@1688169599"),
                ("Link", "</docs>; rel=deprecation"),
                ("ETag", '"v1"'),
                ("Set-Cookie", "session=server-secret"),
                ("X-Secret", "response-secret"),
            ],
            content=b"body-must-not-be-retained",
            request=request,
        )

    clock = FrozenClock(NOW)
    cache = InMemoryCache()
    client = HttpLifecycleClient(
        allowed_hosts=["api.example.test"],
        clock=clock,
        cache=cache,
        transport=httpx.MockTransport(handler),
        minimum_origin_interval_seconds=0,
    )
    result = client.fetch(
        "https://api.example.test/v1?token=top-secret&empty=",
        request_headers={
            "Authorization": "Bearer client-secret",
            "Cookie": "session=client-secret",
        },
    )

    assert result.status is FetchStatus.SUCCESS
    assert result.ok is True
    assert result.url == ("https://api.example.test/v1?token=REDACTED&empty=REDACTED")
    assert result.header_values("Sunset") == ("Wed, 11 Nov 2026 11:11:11 GMT",)
    assert result.header_values("Set-Cookie") == ()
    assert result.header_values("X-Secret") == ()
    assert seen_request is not None
    assert seen_request.headers["authorization"] == "Bearer client-secret"
    assert seen_request.headers["cookie"] == "session=client-secret"
    assert "top-secret" not in repr(result)
    assert "server-secret" not in repr(result)
    client.close()


def test_fresh_ttl_cache_avoids_a_second_request() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            headers={
                "Sunset": "Wed, 11 Nov 2026 11:11:11 GMT",
                "Cache-Control": "max-age=120",
            },
            request=request,
        )

    clock = FrozenClock(NOW)
    client = make_client(
        httpx.MockTransport(handler),
        clock,
        minimum_origin_interval_seconds=0,
    )

    first = client.fetch("https://api.example.test/v1")
    clock.advance(seconds=60)
    second = client.fetch("https://api.example.test/v1")

    assert first.status is FetchStatus.SUCCESS
    assert second.status is FetchStatus.CACHE_HIT
    assert second.fetched_at == NOW
    assert second.stale is False
    assert requests == 1
    client.close()


def test_stale_cache_revalidates_with_etag_and_handles_304() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                headers={
                    "Sunset": "Wed, 11 Nov 2026 11:11:11 GMT",
                    "ETag": '"v1"',
                    "Cache-Control": "max-age=0",
                },
                request=request,
            )
        return httpx.Response(
            304,
            headers={"Cache-Control": "max-age=300"},
            request=request,
        )

    clock = FrozenClock(NOW)
    client = make_client(
        httpx.MockTransport(handler),
        clock,
        minimum_origin_interval_seconds=0,
    )

    first = client.fetch("https://api.example.test/v1")
    clock.advance(seconds=1)
    second = client.fetch("https://api.example.test/v1")

    assert first.status is FetchStatus.SUCCESS
    assert second.status is FetchStatus.NOT_MODIFIED
    assert second.status_code == 200
    assert second.network_status_code == 304
    assert second.fetched_at == NOW
    assert second.validated_at == NOW + timedelta(seconds=1)
    assert second.header_values("sunset") == ("Wed, 11 Nov 2026 11:11:11 GMT",)
    assert requests[1].headers["if-none-match"] == '"v1"'
    client.close()


def test_minimum_origin_interval_never_sleeps_and_returns_stale_cache() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            headers={
                "Deprecation": "@1688169599",
                "Cache-Control": "max-age=0",
            },
            request=request,
        )

    clock = FrozenClock(NOW)
    client = make_client(
        httpx.MockTransport(handler),
        clock,
        minimum_origin_interval_seconds=60,
    )

    assert client.fetch("https://api.example.test/v1").status is FetchStatus.SUCCESS
    limited = client.fetch("https://api.example.test/v1")

    assert limited.status is FetchStatus.RATE_LIMITED
    assert limited.stale is True
    assert limited.next_request_at == NOW + timedelta(seconds=60)
    assert limited.header_values("deprecation") == ("@1688169599",)
    assert requests == 1
    client.close()


@pytest.mark.parametrize("status_code", [429, 503])
def test_retry_after_blocks_origin_without_retrying_or_sleeping(status_code: int) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            status_code,
            headers={"Retry-After": "120", "Set-Cookie": "secret=value"},
            request=request,
        )

    clock = FrozenClock(NOW)
    client = make_client(
        httpx.MockTransport(handler),
        clock,
        minimum_origin_interval_seconds=0,
    )

    first = client.fetch("https://api.example.test/v1")
    second = client.fetch("https://api.example.test/v1")

    assert first.status is FetchStatus.RETRY_LATER
    assert first.next_request_at == NOW + timedelta(seconds=120)
    assert first.header_values("retry-after") == ("120",)
    assert first.header_values("set-cookie") == ()
    assert second.status is FetchStatus.RATE_LIMITED
    assert requests == 1
    client.close()


def test_retry_after_http_date_is_parsed_with_injected_clock() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            headers={"Retry-After": "Thu, 01 Jan 2026 00:05:00 GMT"},
            request=request,
        )

    clock = FrozenClock(NOW)
    client = make_client(
        httpx.MockTransport(handler),
        clock,
        minimum_origin_interval_seconds=0,
    )

    result = client.fetch("https://api.example.test/v1")

    assert result.status is FetchStatus.RETRY_LATER
    assert result.next_request_at == NOW + timedelta(minutes=5)
    client.close()


def test_extreme_cache_and_retry_numbers_are_clamped_before_timedelta() -> None:
    responses = iter(
        (
            {
                "status_code": 200,
                "headers": {
                    "Cache-Control": f"max-age={'9' * 1000}",
                    "Deprecation": "@1688169599",
                },
            },
            {
                "status_code": 429,
                "headers": {"Retry-After": "9" * 1000},
            },
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        response = next(responses)
        return httpx.Response(request=request, **response)

    clock = FrozenClock(NOW)
    client = make_client(
        httpx.MockTransport(handler),
        clock,
        minimum_origin_interval_seconds=0,
    )

    cached = client.fetch("https://api.example.test/cache")
    limited = client.fetch("https://api.example.test/retry")

    assert cached.status is FetchStatus.SUCCESS
    assert cached.expires_at == NOW + timedelta(hours=6)
    assert limited.status is FetchStatus.RETRY_LATER
    assert limited.next_request_at == NOW + timedelta(days=1)
    client.close()


@pytest.mark.parametrize(
    ("url", "error_code"),
    [
        ("ftp://api.example.test/v1", "unsupported_scheme"),
        ("http://api.example.test/v1", "https_required"),
        ("https://user:password@api.example.test/v1", "userinfo_forbidden"),
        ("https://api.example.test/v1#fragment", "fragment_forbidden"),
        ("https://other.example.test/v1", "host_not_allowed"),
    ],
)
def test_policy_rejections_never_reach_transport(url: str, error_code: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("blocked URLs must not reach the transport")

    client = make_client(httpx.MockTransport(handler), FrozenClock(NOW))

    result = client.fetch(url)

    assert result.status is FetchStatus.BLOCKED
    assert result.error_code == error_code
    assert "password" not in result.url
    client.close()


def test_wildcard_matches_subdomains_but_not_apex() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(204, request=request)

    client = HttpLifecycleClient(
        allowed_hosts=["*.example.test"],
        clock=FrozenClock(NOW),
        transport=httpx.MockTransport(handler),
        minimum_origin_interval_seconds=0,
    )

    nested = client.fetch("https://v1.api.example.test/status")
    apex = client.fetch("https://example.test/status")

    assert nested.status is FetchStatus.SUCCESS
    assert apex.status is FetchStatus.BLOCKED
    assert apex.error_code == "host_not_allowed"
    assert requests == ["https://v1.api.example.test/status"]
    client.close()


def test_plain_http_is_available_only_for_explicit_loopback_fixture_mode() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, request=request)

    transport = httpx.MockTransport(handler)
    blocked = HttpLifecycleClient(
        allowed_hosts=["127.0.0.1"],
        clock=FrozenClock(NOW),
        transport=transport,
    )
    allowed = HttpLifecycleClient(
        allowed_hosts=["127.0.0.1"],
        clock=FrozenClock(NOW),
        transport=transport,
        allow_loopback=True,
    )

    assert blocked.fetch("http://127.0.0.1:8080/lifecycle").status is FetchStatus.BLOCKED
    assert allowed.fetch("http://127.0.0.1:8080/lifecycle").status is FetchStatus.SUCCESS
    blocked.close()
    allowed.close()


def test_redirect_is_not_followed_and_location_is_not_retained() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            302,
            headers={"Location": "https://evil.example/steal?token=secret"},
            request=request,
        )

    client = make_client(httpx.MockTransport(handler), FrozenClock(NOW))

    result = client.fetch("https://api.example.test/v1")

    assert result.status is FetchStatus.REDIRECT_BLOCKED
    assert result.header_values("location") == ()
    assert "secret" not in repr(result)
    assert requests == 1
    client.close()


def test_timeout_becomes_safe_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream contained a secret", request=request)

    client = make_client(httpx.MockTransport(handler), FrozenClock(NOW))

    result = client.fetch("https://api.example.test/v1?api_key=hidden")

    assert result.status is FetchStatus.TIMEOUT
    assert result.error_code == "request_timeout"
    assert result.url == "https://api.example.test/v1?api_key=REDACTED"
    assert "secret" not in repr(result)
    assert "hidden" not in repr(result)
    client.close()


def test_no_store_response_is_never_cached() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store", "Deprecation": "@1"},
            request=request,
        )

    client = make_client(
        httpx.MockTransport(handler),
        FrozenClock(NOW),
        minimum_origin_interval_seconds=0,
    )

    first = client.fetch("https://api.example.test/v1")
    second = client.fetch("https://api.example.test/v1")

    assert first.status is FetchStatus.SUCCESS
    assert first.expires_at is None
    assert second.status is FetchStatus.SUCCESS
    assert requests == 2
    client.close()


def test_redaction_masks_duplicate_blank_and_flag_query_components() -> None:
    safe = redact_query_values("https://user:password@api.example.test/v1?a=one&a=two&blank=&flag")

    assert safe == (
        "https://api.example.test/v1?a=REDACTED&a=REDACTED&blank=REDACTED&flag=REDACTED"
    )
    assert "password" not in safe
