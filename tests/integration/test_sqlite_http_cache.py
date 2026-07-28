from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import httpx

from sunset_sentinel_api.adapters.http_client import FetchStatus, HttpLifecycleClient
from sunset_sentinel_api.adapters.sqlite_http_cache import (
    SQLiteHttpCache,
    SQLiteRequestPacingStore,
)
from sunset_sentinel_api.adapters.sqlite_repository import SQLiteRepository
from sunset_sentinel_api.clock import FrozenClock


def test_sqlite_cache_survives_client_recreation_without_body_or_query_secret(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={
                "Sunset": "Wed, 30 Sep 2026 23:59:59 GMT",
                "Cache-Control": "max-age=3600",
                "Set-Cookie": "session=must-not-persist",
            },
            content=b"response-body-must-not-persist",
            request=request,
        )

    now = datetime(2026, 7, 23, tzinfo=UTC)
    with SQLiteRepository(tmp_path / "cache.db", clock=FrozenClock(now)) as repository:
        repository.initialize()
        cache = SQLiteHttpCache(repository)
        first_client = HttpLifecycleClient(
            allowed_hosts=("api.example.test",),
            clock=FrozenClock(now),
            cache=cache,
            transport=httpx.MockTransport(handler),
        )
        first = first_client.fetch("https://api.example.test/v1?api_key=top-secret")
        first_client.close()

        second_client = HttpLifecycleClient(
            allowed_hosts=("api.example.test",),
            clock=FrozenClock(now),
            cache=cache,
            transport=httpx.MockTransport(handler),
        )
        second = second_client.fetch("https://api.example.test/v1?api_key=top-secret")
        second_client.close()

        request_url = str(httpx.URL("https://api.example.test/v1?api_key=top-secret"))
        stored = repository.get_http_cache(sha256(request_url.encode("utf-8")).hexdigest())

    assert first.status is FetchStatus.SUCCESS
    assert second.status is FetchStatus.CACHE_HIT
    assert calls == 1
    assert second.header_values("set-cookie") == ()
    assert "top-secret" not in second.url
    assert stored is not None
    assert "top-secret" not in stored.redacted_url
    assert "response-body" not in repr(stored)


def test_request_interval_and_retry_after_survive_client_recreation(
    tmp_path: Path,
) -> None:
    calls = 0

    def success_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={
                "Deprecation": "@1780000000",
                "Cache-Control": "no-store",
            },
            request=request,
        )

    now = datetime(2026, 7, 23, tzinfo=UTC)
    database = tmp_path / "pacing.db"
    with SQLiteRepository(database, clock=FrozenClock(now)) as repository:
        pacing = SQLiteRequestPacingStore(repository)
        first_client = HttpLifecycleClient(
            allowed_hosts=("api.example.test",),
            clock=FrozenClock(now),
            cache=SQLiteHttpCache(repository),
            request_pacing=pacing,
            transport=httpx.MockTransport(success_handler),
            minimum_origin_interval_seconds=60,
        )
        first = first_client.fetch("https://api.example.test/no-store")
        first_client.close()

    with SQLiteRepository(database, clock=FrozenClock(now)) as repository:
        second_client = HttpLifecycleClient(
            allowed_hosts=("api.example.test",),
            clock=FrozenClock(now),
            cache=SQLiteHttpCache(repository),
            request_pacing=SQLiteRequestPacingStore(repository),
            transport=httpx.MockTransport(success_handler),
            minimum_origin_interval_seconds=60,
        )
        second = second_client.fetch("https://api.example.test/no-store")
        second_client.close()

    assert first.status is FetchStatus.SUCCESS
    assert second.status is FetchStatus.RATE_LIMITED
    assert second.next_request_at == now + timedelta(seconds=60)
    assert calls == 1

    def retry_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"}, request=request)

    after_interval = now + timedelta(seconds=61)
    with SQLiteRepository(database, clock=FrozenClock(after_interval)) as repository:
        retry_client = HttpLifecycleClient(
            allowed_hosts=("api.example.test",),
            clock=FrozenClock(after_interval),
            request_pacing=SQLiteRequestPacingStore(repository),
            transport=httpx.MockTransport(retry_handler),
            minimum_origin_interval_seconds=60,
        )
        retry = retry_client.fetch("https://api.example.test/retry")
        retry_client.close()

    during_retry_after = now + timedelta(seconds=90)
    with SQLiteRepository(
        database,
        clock=FrozenClock(during_retry_after),
    ) as repository:
        blocked_client = HttpLifecycleClient(
            allowed_hosts=("api.example.test",),
            clock=FrozenClock(during_retry_after),
            request_pacing=SQLiteRequestPacingStore(repository),
            transport=httpx.MockTransport(retry_handler),
            minimum_origin_interval_seconds=60,
        )
        blocked = blocked_client.fetch("https://api.example.test/retry")
        blocked_client.close()

    assert retry.status is FetchStatus.RETRY_LATER
    assert retry.next_request_at == after_interval + timedelta(seconds=120)
    assert blocked.status is FetchStatus.RATE_LIMITED
    assert blocked.next_request_at == retry.next_request_at
    assert calls == 2
