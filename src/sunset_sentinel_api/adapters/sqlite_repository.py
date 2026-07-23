"""Transactional SQLite persistence for lifecycle evidence and HTTP metadata."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Literal, cast

from pydantic import BaseModel, ValidationError

from sunset_sentinel_api.clock import Clock, SystemClock
from sunset_sentinel_api.domain.models import (
    Consumer,
    ConsumerDependency,
    LifecycleSignal,
    as_utc,
    validate_http_url,
)

ChangeType = Literal["discovered", "updated", "withdrawn"]
HeaderTuple = tuple[tuple[str, str], ...]

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS consumers (
        consumer_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        CHECK (first_seen_at <= last_seen_at)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS lifecycle_signals (
        signal_key TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        active INTEGER NOT NULL CHECK (active IN (0, 1)),
        CHECK (first_seen_at <= last_seen_at)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS consumer_dependencies (
        consumer_id TEXT NOT NULL
            REFERENCES consumers(consumer_id) ON UPDATE CASCADE ON DELETE CASCADE,
        endpoint_key TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        PRIMARY KEY (consumer_id, endpoint_key),
        CHECK (first_seen_at <= last_seen_at)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS changes (
        change_id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_key TEXT NOT NULL
            REFERENCES lifecycle_signals(signal_key) ON UPDATE CASCADE ON DELETE CASCADE,
        change_type TEXT NOT NULL
            CHECK (change_type IN ('discovered', 'updated', 'withdrawn')),
        recorded_at TEXT NOT NULL,
        previous_json TEXT CHECK (
            previous_json IS NULL OR json_valid(previous_json)
        ),
        current_json TEXT NOT NULL CHECK (json_valid(current_json))
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS http_cache (
        cache_key TEXT PRIMARY KEY,
        status_code INTEGER NOT NULL CHECK (status_code BETWEEN 100 AND 599),
        headers_json TEXT NOT NULL CHECK (json_valid(headers_json)),
        fetched_at TEXT NOT NULL,
        validated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        etag TEXT,
        last_modified TEXT,
        redacted_url TEXT NOT NULL,
        CHECK (fetched_at <= validated_at)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS host_request_state (
        host TEXT PRIMARY KEY,
        last_requested_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_changes_signal_and_time
    ON changes(signal_key, recorded_at, change_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_http_cache_expiration
    ON http_cache(expires_at, fetched_at)
    """,
)


class RepositoryError(RuntimeError):
    """Base error for storage setup and persisted-data failures."""


class RepositoryDataError(RepositoryError):
    """Persisted data could not be safely restored into a domain model."""


@dataclass(frozen=True, slots=True)
class StoredLifecycleSignal:
    """A lifecycle signal together with its durable observation interval."""

    signal: LifecycleSignal
    first_seen_at: datetime
    last_seen_at: datetime

    @property
    def signal_key(self) -> str:
        """Return the stable signal identity."""

        return self.signal.signal_key


@dataclass(frozen=True, slots=True)
class LifecycleChange:
    """One append-only material change to a lifecycle signal."""

    change_id: int
    signal_key: str
    change_type: ChangeType
    recorded_at: datetime
    previous: LifecycleSignal | None
    current: LifecycleSignal


@dataclass(frozen=True, slots=True)
class HttpCacheEntry:
    """Bounded cache metadata; response bodies are deliberately unsupported."""

    key: str
    status_code: int
    headers: HeaderTuple
    fetched_at: datetime
    validated_at: datetime
    expires_at: datetime
    etag: str | None
    last_modified: str | None
    redacted_url: str


class SQLiteRepository:
    """Persist immutable domain values with explicit, rollback-safe transactions."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Clock | None = None,
        busy_timeout_ms: int = 5_000,
        max_cache_entries: int = 256,
        max_cache_entry_bytes: int = 65_536,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        if max_cache_entries <= 0:
            raise ValueError("max_cache_entries must be positive")
        if max_cache_entry_bytes <= 0:
            raise ValueError("max_cache_entry_bytes must be positive")

        self._database_path = Path(database_path)
        if str(database_path) != ":memory:":
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock if clock is not None else SystemClock()
        self._busy_timeout_ms = busy_timeout_ms
        self._max_cache_entries = max_cache_entries
        self._max_cache_entry_bytes = max_cache_entry_bytes
        self._lock = RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            str(self._database_path),
            timeout=busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self.initialize()

    @property
    def database_path(self) -> Path:
        """Return the configured database path."""

        return self._database_path

    @property
    def busy_timeout_ms(self) -> int:
        """Return the configured SQLite lock wait in milliseconds."""

        return self._busy_timeout_ms

    def __enter__(self) -> SQLiteRepository:
        self._ensure_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def initialize(self) -> None:
        """Create the schema and required connection pragmas idempotently."""

        self._ensure_open()
        with self._lock:
            try:
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
                self._connection.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA_STATEMENTS:
                    self._connection.execute(statement)
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

            foreign_keys = self._connection.execute("PRAGMA foreign_keys").fetchone()
            if foreign_keys is None or _row_index_int(foreign_keys, 0) != 1:
                raise RepositoryError("SQLite foreign key enforcement could not be enabled")

    def close(self) -> None:
        """Close the underlying connection; repeated calls are harmless."""

        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def upsert_consumer(
        self,
        consumer: Consumer,
        *,
        observed_at: datetime | None = None,
    ) -> Consumer:
        """Insert or replace a consumer while retaining its discovery interval."""

        timestamp = self._timestamp(observed_at)
        payload = _model_json(consumer)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT first_seen_at, last_seen_at
                FROM consumers
                WHERE consumer_id = ?
                """,
                (consumer.id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO consumers(
                        consumer_id, payload_json, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (consumer.id, payload, timestamp, timestamp),
                )
            else:
                last_seen = max(
                    _parse_datetime(_row_str(existing, "last_seen_at")),
                    _parse_datetime(timestamp),
                )
                connection.execute(
                    """
                    UPDATE consumers
                    SET payload_json = ?, last_seen_at = ?
                    WHERE consumer_id = ?
                    """,
                    (payload, _format_datetime(last_seen), consumer.id),
                )
        return consumer

    def upsert_dependency(
        self,
        dependency: ConsumerDependency,
        *,
        observed_at: datetime | None = None,
    ) -> ConsumerDependency:
        """Insert or replace a dependency edge, enforcing its consumer foreign key."""

        timestamp = self._timestamp(observed_at)
        payload = _model_json(dependency)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT last_seen_at
                FROM consumer_dependencies
                WHERE consumer_id = ? AND endpoint_key = ?
                """,
                (dependency.consumer_id, dependency.endpoint_key),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO consumer_dependencies(
                        consumer_id,
                        endpoint_key,
                        payload_json,
                        first_seen_at,
                        last_seen_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        dependency.consumer_id,
                        dependency.endpoint_key,
                        payload,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                last_seen = max(
                    _parse_datetime(_row_str(existing, "last_seen_at")),
                    _parse_datetime(timestamp),
                )
                connection.execute(
                    """
                    UPDATE consumer_dependencies
                    SET payload_json = ?, last_seen_at = ?
                    WHERE consumer_id = ? AND endpoint_key = ?
                    """,
                    (
                        payload,
                        _format_datetime(last_seen),
                        dependency.consumer_id,
                        dependency.endpoint_key,
                    ),
                )
        return dependency

    def upsert_signal(self, signal: LifecycleSignal) -> StoredLifecycleSignal:
        """Reconcile a signal and append a change only for a material transition."""

        payload = _model_json(signal)
        observed_at = signal.observed_at
        observed_text = _format_datetime(observed_at)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT payload_json, first_seen_at, last_seen_at
                FROM lifecycle_signals
                WHERE signal_key = ?
                """,
                (signal.signal_key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO lifecycle_signals(
                        signal_key,
                        payload_json,
                        first_seen_at,
                        last_seen_at,
                        active
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        signal.signal_key,
                        payload,
                        observed_text,
                        observed_text,
                        int(signal.active),
                    ),
                )
                self._append_change(
                    connection,
                    signal=signal,
                    change_type="discovered",
                    previous_json=None,
                    current_json=payload,
                )
                return StoredLifecycleSignal(
                    signal=signal,
                    first_seen_at=observed_at,
                    last_seen_at=observed_at,
                )

            previous_payload = _row_str(existing, "payload_json")
            previous = _restore_signal(previous_payload)
            first_seen = _parse_datetime(_row_str(existing, "first_seen_at"))
            previous_last_seen = _parse_datetime(_row_str(existing, "last_seen_at"))

            if observed_at < previous_last_seen:
                return StoredLifecycleSignal(
                    signal=previous,
                    first_seen_at=first_seen,
                    last_seen_at=previous_last_seen,
                )

            material_change = _material_signal_payload(previous) != _material_signal_payload(signal)
            if material_change:
                change_type: ChangeType = (
                    "withdrawn" if previous.active and not signal.active else "updated"
                )
                self._append_change(
                    connection,
                    signal=signal,
                    change_type=change_type,
                    previous_json=previous_payload,
                    current_json=payload,
                )

            connection.execute(
                """
                UPDATE lifecycle_signals
                SET payload_json = ?, last_seen_at = ?, active = ?
                WHERE signal_key = ?
                """,
                (payload, observed_text, int(signal.active), signal.signal_key),
            )
            return StoredLifecycleSignal(
                signal=signal,
                first_seen_at=first_seen,
                last_seen_at=observed_at,
            )

    def get_consumer(self, consumer_id: str) -> Consumer | None:
        """Return one consumer by stable ID."""

        with self._read_lock():
            row = self._connection.execute(
                "SELECT payload_json FROM consumers WHERE consumer_id = ?",
                (consumer_id,),
            ).fetchone()
        return None if row is None else _restore_consumer(_row_str(row, "payload_json"))

    def list_consumers(self) -> tuple[Consumer, ...]:
        """List consumers in deterministic ID order."""

        with self._read_lock():
            rows = self._connection.execute(
                "SELECT payload_json FROM consumers ORDER BY consumer_id"
            ).fetchall()
        return tuple(_restore_consumer(_row_str(row, "payload_json")) for row in rows)

    def list_dependencies(self) -> tuple[ConsumerDependency, ...]:
        """List consumer-to-endpoint edges in deterministic key order."""

        with self._read_lock():
            rows = self._connection.execute(
                """
                SELECT payload_json
                FROM consumer_dependencies
                ORDER BY consumer_id, endpoint_key
                """
            ).fetchall()
        return tuple(_restore_dependency(_row_str(row, "payload_json")) for row in rows)

    def get_signal(self, signal_key: str) -> StoredLifecycleSignal | None:
        """Return one signal and its first/last-seen interval."""

        with self._read_lock():
            row = self._connection.execute(
                """
                SELECT payload_json, first_seen_at, last_seen_at
                FROM lifecycle_signals
                WHERE signal_key = ?
                """,
                (signal_key,),
            ).fetchone()
        return None if row is None else _stored_signal(row)

    def list_signal_records(
        self,
        *,
        active_only: bool = False,
    ) -> tuple[StoredLifecycleSignal, ...]:
        """List persisted signal records, optionally excluding withdrawals."""

        where = "WHERE active = 1" if active_only else ""
        with self._read_lock():
            rows = self._connection.execute(
                f"""
                SELECT payload_json, first_seen_at, last_seen_at
                FROM lifecycle_signals
                {where}
                ORDER BY signal_key
                """  # noqa: S608
            ).fetchall()
        return tuple(_stored_signal(row) for row in rows)

    def list_signals(self, *, active_only: bool = False) -> tuple[LifecycleSignal, ...]:
        """List restored domain signals in deterministic key order."""

        return tuple(record.signal for record in self.list_signal_records(active_only=active_only))

    def list_changes(self, *, signal_key: str | None = None) -> tuple[LifecycleChange, ...]:
        """List append-only changes, globally or for one signal."""

        parameters: tuple[str, ...] = ()
        where = ""
        if signal_key is not None:
            where = "WHERE signal_key = ?"
            parameters = (signal_key,)
        with self._read_lock():
            rows = self._connection.execute(
                f"""
                SELECT
                    change_id,
                    signal_key,
                    change_type,
                    recorded_at,
                    previous_json,
                    current_json
                FROM changes
                {where}
                ORDER BY change_id
                """,  # noqa: S608
                parameters,
            ).fetchall()
        return tuple(_restore_change(row) for row in rows)

    def put_http_cache(
        self,
        key: str,
        *,
        status_code: int,
        headers: Mapping[str, str] | Sequence[tuple[str, str]],
        fetched_at: datetime,
        expires_at: datetime,
        redacted_url: str,
        validated_at: datetime | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> HttpCacheEntry:
        """Insert cache metadata and evict the oldest entries beyond the bound."""

        cache_key = _validate_cache_key(key)
        if not 100 <= status_code <= 599:
            raise ValueError("status_code must be between 100 and 599")
        normalized_headers = _normalize_headers(headers)
        normalized_fetched = as_utc(fetched_at, field_name="fetched_at")
        normalized_validated = (
            normalized_fetched
            if validated_at is None
            else as_utc(validated_at, field_name="validated_at")
        )
        normalized_expires = as_utc(expires_at, field_name="expires_at")
        if normalized_validated < normalized_fetched:
            raise ValueError("validated_at must not be before fetched_at")
        safe_url = validate_http_url(redacted_url, field_name="redacted_url")
        normalized_etag = _safe_optional_metadata(etag, field_name="etag")
        normalized_modified = _safe_optional_metadata(
            last_modified,
            field_name="last_modified",
        )
        if normalized_etag is None:
            normalized_etag = _last_header(normalized_headers, "etag")
        if normalized_modified is None:
            normalized_modified = _last_header(normalized_headers, "last-modified")

        headers_json = json.dumps(
            normalized_headers,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        entry_size = sum(
            len(value.encode("utf-8"))
            for value in (
                cache_key,
                headers_json,
                normalized_etag or "",
                normalized_modified or "",
                safe_url,
            )
        )
        if entry_size > self._max_cache_entry_bytes:
            raise ValueError("HTTP cache metadata exceeds max_cache_entry_bytes")

        entry = HttpCacheEntry(
            key=cache_key,
            status_code=status_code,
            headers=normalized_headers,
            fetched_at=normalized_fetched,
            validated_at=normalized_validated,
            expires_at=normalized_expires,
            etag=normalized_etag,
            last_modified=normalized_modified,
            redacted_url=safe_url,
        )
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO http_cache(
                    cache_key,
                    status_code,
                    headers_json,
                    fetched_at,
                    validated_at,
                    expires_at,
                    etag,
                    last_modified,
                    redacted_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    status_code = excluded.status_code,
                    headers_json = excluded.headers_json,
                    fetched_at = excluded.fetched_at,
                    validated_at = excluded.validated_at,
                    expires_at = excluded.expires_at,
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    redacted_url = excluded.redacted_url
                """,
                (
                    entry.key,
                    entry.status_code,
                    headers_json,
                    _format_datetime(entry.fetched_at),
                    _format_datetime(entry.validated_at),
                    _format_datetime(entry.expires_at),
                    entry.etag,
                    entry.last_modified,
                    entry.redacted_url,
                ),
            )
            self._trim_http_cache(connection)
        return entry

    def get_http_cache(self, key: str) -> HttpCacheEntry | None:
        """Return cached metadata without applying a freshness policy."""

        cache_key = _validate_cache_key(key)
        with self._read_lock():
            row = self._connection.execute(
                """
                SELECT
                    cache_key,
                    status_code,
                    headers_json,
                    fetched_at,
                    validated_at,
                    expires_at,
                    etag,
                    last_modified,
                    redacted_url
                FROM http_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        return None if row is None else _restore_http_cache(row)

    def delete_http_cache(self, key: str) -> bool:
        """Delete one cache entry and report whether it existed."""

        cache_key = _validate_cache_key(key)
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM http_cache WHERE cache_key = ?",
                (cache_key,),
            )
        affected = int(cursor.rowcount)
        return affected > 0

    def prune_http_cache(self, *, now: datetime | None = None) -> int:
        """Delete entries whose expiry is at or before the supplied instant."""

        timestamp = self._timestamp(now)
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM http_cache WHERE expires_at <= ?",
                (timestamp,),
            )
        return int(cursor.rowcount)

    def get_host_last_request(self, host: str) -> datetime | None:
        """Return the most recent persisted request time for a host or origin."""

        normalized = _normalize_host_key(host)
        with self._read_lock():
            row = self._connection.execute(
                """
                SELECT last_requested_at
                FROM host_request_state
                WHERE host = ?
                """,
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        return _parse_datetime(_row_str(row, "last_requested_at"))

    def set_host_last_request(
        self,
        host: str,
        *,
        requested_at: datetime | None = None,
    ) -> datetime:
        """Monotonically update a host's last-request instant."""

        normalized = _normalize_host_key(host)
        timestamp = self._timestamp(requested_at)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO host_request_state(host, last_requested_at)
                VALUES (?, ?)
                ON CONFLICT(host) DO UPDATE SET
                    last_requested_at = max(
                        host_request_state.last_requested_at,
                        excluded.last_requested_at
                    )
                """,
                (normalized, timestamp),
            )
            row = connection.execute(
                """
                SELECT last_requested_at
                FROM host_request_state
                WHERE host = ?
                """,
                (normalized,),
            ).fetchone()
        if row is None:
            raise RepositoryError("host request state disappeared during its transaction")
        return _parse_datetime(_row_str(row, "last_requested_at"))

    def _append_change(
        self,
        connection: sqlite3.Connection,
        *,
        signal: LifecycleSignal,
        change_type: ChangeType,
        previous_json: str | None,
        current_json: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO changes(
                signal_key,
                change_type,
                recorded_at,
                previous_json,
                current_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                signal.signal_key,
                change_type,
                _format_datetime(signal.observed_at),
                previous_json,
                current_json,
            ),
        )

    def _trim_http_cache(self, connection: sqlite3.Connection) -> None:
        stale_rows = connection.execute(
            """
            SELECT cache_key
            FROM http_cache
            ORDER BY fetched_at DESC, cache_key DESC
            LIMIT -1 OFFSET ?
            """,
            (self._max_cache_entries,),
        ).fetchall()
        connection.executemany(
            "DELETE FROM http_cache WHERE cache_key = ?",
            ((_row_str(row, "cache_key"),) for row in stale_rows),
        )

    def _timestamp(self, value: datetime | None) -> str:
        current = self._clock.now() if value is None else value
        return _format_datetime(as_utc(current, field_name="timestamp"))

    def _ensure_open(self) -> None:
        if self._closed:
            raise RepositoryError("SQLite repository is closed")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._ensure_open()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @contextmanager
    def _read_lock(self) -> Iterator[None]:
        self._ensure_open()
        with self._lock:
            yield


def _model_json(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _material_signal_payload(signal: LifecycleSignal) -> dict[str, object]:
    payload = signal.model_dump(mode="json")
    payload.pop("observed_at", None)
    return payload


def _restore_consumer(payload: str) -> Consumer:
    try:
        return Consumer.model_validate_json(payload)
    except (ValidationError, ValueError) as exc:
        raise RepositoryDataError("stored consumer payload is invalid") from exc


def _restore_dependency(payload: str) -> ConsumerDependency:
    try:
        return ConsumerDependency.model_validate_json(payload)
    except (ValidationError, ValueError) as exc:
        raise RepositoryDataError("stored dependency payload is invalid") from exc


def _restore_signal(payload: str) -> LifecycleSignal:
    try:
        return LifecycleSignal.model_validate_json(payload)
    except (ValidationError, ValueError) as exc:
        raise RepositoryDataError("stored lifecycle signal payload is invalid") from exc


def _stored_signal(row: sqlite3.Row) -> StoredLifecycleSignal:
    return StoredLifecycleSignal(
        signal=_restore_signal(_row_str(row, "payload_json")),
        first_seen_at=_parse_datetime(_row_str(row, "first_seen_at")),
        last_seen_at=_parse_datetime(_row_str(row, "last_seen_at")),
    )


def _restore_change(row: sqlite3.Row) -> LifecycleChange:
    raw_type = _row_str(row, "change_type")
    if raw_type not in {"discovered", "updated", "withdrawn"}:
        raise RepositoryDataError("stored change type is invalid")
    previous_value: object = row["previous_json"]
    if previous_value is not None and not isinstance(previous_value, str):
        raise RepositoryDataError("stored previous change payload is not text")
    return LifecycleChange(
        change_id=_row_int(row, "change_id"),
        signal_key=_row_str(row, "signal_key"),
        change_type=cast(ChangeType, raw_type),
        recorded_at=_parse_datetime(_row_str(row, "recorded_at")),
        previous=None if previous_value is None else _restore_signal(previous_value),
        current=_restore_signal(_row_str(row, "current_json")),
    )


def _restore_http_cache(row: sqlite3.Row) -> HttpCacheEntry:
    return HttpCacheEntry(
        key=_row_str(row, "cache_key"),
        status_code=_row_int(row, "status_code"),
        headers=_restore_headers(_row_str(row, "headers_json")),
        fetched_at=_parse_datetime(_row_str(row, "fetched_at")),
        validated_at=_parse_datetime(_row_str(row, "validated_at")),
        expires_at=_parse_datetime(_row_str(row, "expires_at")),
        etag=_row_optional_str(row, "etag"),
        last_modified=_row_optional_str(row, "last_modified"),
        redacted_url=_row_str(row, "redacted_url"),
    )


def _normalize_headers(
    headers: Mapping[str, str] | Sequence[tuple[str, str]],
) -> HeaderTuple:
    items = headers.items() if isinstance(headers, Mapping) else headers
    normalized: list[tuple[str, str]] = []
    for name, value in items:
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("HTTP cache headers must contain string names and values")
        field = name.casefold()
        if not _HEADER_NAME_RE.fullmatch(field):
            raise ValueError("HTTP cache contains an invalid header name")
        if "\r" in value or "\n" in value or "\x00" in value:
            raise ValueError("HTTP cache contains an unsafe header value")
        normalized.append((field, value))
    return tuple(normalized)


def _restore_headers(payload: str) -> HeaderTuple:
    try:
        raw: object = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RepositoryDataError("stored HTTP headers are not valid JSON") from exc
    if not isinstance(raw, list):
        raise RepositoryDataError("stored HTTP headers must be a list")
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise RepositoryDataError("stored HTTP header entry is invalid")
        pairs.append((item[0], item[1]))
    try:
        return _normalize_headers(pairs)
    except (TypeError, ValueError) as exc:
        raise RepositoryDataError("stored HTTP headers failed validation") from exc


def _last_header(headers: HeaderTuple, name: str) -> str | None:
    normalized = name.casefold()
    values = [value for field, value in headers if field == normalized]
    return values[-1] if values else None


def _safe_optional_metadata(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not value or len(value) > 4_096 or "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"{field_name} contains unsafe metadata")
    return value


def _validate_cache_key(value: str) -> str:
    if not value or len(value) > 512 or _CONTROL_RE.search(value):
        raise ValueError("cache key must be a non-empty, control-free string")
    return value


def _normalize_host_key(value: str) -> str:
    normalized = value.strip().rstrip(".").casefold()
    if (
        not normalized
        or len(normalized) > 512
        or _CONTROL_RE.search(normalized)
        or any(character.isspace() for character in normalized)
        or "/" in normalized
        or "\\" in normalized
        or "@" in normalized
    ):
        raise ValueError("host must be a safe hostname or origin key")
    try:
        return normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("host contains invalid internationalized characters") from exc


def _format_datetime(value: datetime) -> str:
    return as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RepositoryDataError("stored timestamp is not valid ISO 8601") from exc
    try:
        return as_utc(parsed)
    except ValueError as exc:
        raise RepositoryDataError("stored timestamp must include a timezone") from exc


def _row_str(row: sqlite3.Row, key: str) -> str:
    value: object = row[key]
    if not isinstance(value, str):
        raise RepositoryDataError(f"stored {key} is not text")
    return value


def _row_optional_str(row: sqlite3.Row, key: str) -> str | None:
    value: object = row[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise RepositoryDataError(f"stored {key} is not text")
    return value


def _row_int(row: sqlite3.Row, key: str) -> int:
    value: object = row[key]
    if not isinstance(value, int):
        raise RepositoryDataError(f"stored {key} is not an integer")
    return value


def _row_index_int(row: sqlite3.Row, index: int) -> int:
    value: object = row[index]
    if not isinstance(value, int):
        raise RepositoryDataError("stored pragma value is not an integer")
    return value
