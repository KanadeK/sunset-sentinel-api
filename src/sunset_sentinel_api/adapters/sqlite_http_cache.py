"""Bridge the HTTP cache port to the durable SQLite repository."""

from __future__ import annotations

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
