"""Bridge the HTTP cache port to the durable SQLite repository."""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256

from sunset_sentinel_api.adapters.http_client import CachedLifecycleResponse
from sunset_sentinel_api.adapters.sqlite_repository import SQLiteRepository


class SQLiteHttpCache:
    """Persist lifecycle response metadata without storing response bodies."""

    def __init__(self, repository: SQLiteRepository) -> None:
        self._repository = repository

    def get(self, key: str) -> CachedLifecycleResponse | None:
        """Restore one HTTP cache entry."""

        entry = self._repository.get_http_cache(key)
        if entry is None:
            return None
        return CachedLifecycleResponse(
            key=entry.key,
            url=entry.redacted_url,
            status_code=entry.status_code,
            headers=entry.headers,
            fetched_at=entry.fetched_at,
            validated_at=entry.validated_at,
            expires_at=entry.expires_at,
        )

    def put(self, entry: CachedLifecycleResponse) -> None:
        """Persist one HTTP cache entry with its already-redacted display URL."""

        self._repository.put_http_cache(
            entry.key,
            status_code=entry.status_code,
            headers=entry.headers,
            fetched_at=entry.fetched_at,
            validated_at=entry.validated_at,
            expires_at=entry.expires_at,
            redacted_url=entry.url,
        )


class SQLiteRequestPacingStore:
    """Coordinate per-origin request intervals and Retry-After across processes."""

    def __init__(self, repository: SQLiteRepository) -> None:
        self._repository = repository

    def claim(
        self,
        origin: str,
        *,
        requested_at: datetime,
        minimum_interval: timedelta,
    ) -> datetime | None:
        """Reserve an origin request using one SQLite immediate transaction."""

        return self._repository.claim_origin_request(
            _origin_key(origin),
            requested_at=requested_at,
            minimum_interval=minimum_interval,
        )

    def defer(self, origin: str, *, until: datetime) -> None:
        """Persist the latest Retry-After deadline for an origin."""

        self._repository.defer_origin_request(_origin_key(origin), until=until)


def _origin_key(origin: str) -> str:
    return f"sha256:{sha256(origin.encode('utf-8')).hexdigest()}"
