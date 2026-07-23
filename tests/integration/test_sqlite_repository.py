from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sunset_sentinel_api.adapters.sqlite_repository import (
    RepositoryDataError,
    SQLiteRepository,
)
from sunset_sentinel_api.clock import FrozenClock
from sunset_sentinel_api.domain import (
    Consumer,
    ConsumerDependency,
    Criticality,
    EndpointRef,
    LifecycleSignal,
    ScopeKind,
    SignalSource,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ENDPOINT = EndpointRef(
    target_id="billing-api",
    method="GET",
    path="/v1/invoices/{invoice_id}",
    operation_id="getInvoice",
)


def make_signal(
    observed_at: datetime,
    *,
    sunset_at: datetime = datetime(2026, 9, 1, tzinfo=UTC),
    active: bool = True,
    raw: str = "original",
) -> LifecycleSignal:
    return LifecycleSignal(
        signal_key="billing-api:get-invoice:http",
        target_id="billing-api",
        source=SignalSource.HTTP_HEADER,
        source_ref="https://api.example.test/v1/invoices/123",
        scope=ScopeKind.ENDPOINT,
        endpoint=ENDPOINT,
        deprecated=True,
        deprecation_at=datetime(2026, 2, 1, tzinfo=UTC),
        sunset_at=sunset_at,
        documentation_url="https://docs.example.test/migrate",
        replacement="/v2/invoices/{invoice_id}",
        observed_at=observed_at,
        active=active,
        raw_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    )


def test_schema_is_idempotent_and_domain_models_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "sentinel.sqlite"
    clock = FrozenClock(NOW)
    consumer = Consumer(
        id="checkout",
        name="Checkout",
        criticality=Criticality.CRITICAL,
        owner="payments",
        repository_path="services/checkout",
        tags=("python", "customer-facing"),
    )
    dependency = ConsumerDependency(
        consumer_id=consumer.id,
        endpoint_key=ENDPOINT.key,
        evidence="src/billing.py:42",
    )

    with SQLiteRepository(database, clock=clock) as repository:
        repository.initialize()
        assert repository._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert repository._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert (
            repository._connection.execute("PRAGMA busy_timeout").fetchone()[0]
            == repository.busy_timeout_ms
        )

        repository.upsert_consumer(consumer)
        repository.upsert_dependency(dependency)
        first = repository.upsert_signal(make_signal(NOW))

        assert first.first_seen_at == NOW
        assert first.last_seen_at == NOW
        assert repository.list_consumers() == (consumer,)
        assert repository.list_dependencies() == (dependency,)
        assert repository.list_signals() == (make_signal(NOW),)

    with SQLiteRepository(database, clock=clock) as reopened:
        assert reopened.get_consumer("checkout") == consumer
        restored = reopened.get_signal("billing-api:get-invoice:http")
        assert restored is not None
        assert restored.signal == make_signal(NOW)
        assert restored.first_seen_at == NOW
        assert restored.last_seen_at == NOW


def test_signal_upsert_preserves_seen_interval_and_appends_material_changes(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "signals.sqlite", clock=FrozenClock(NOW))
    first = make_signal(NOW)
    repeated = make_signal(NOW + timedelta(hours=1))
    updated = make_signal(
        NOW + timedelta(hours=2),
        sunset_at=datetime(2026, 8, 1, tzinfo=UTC),
        raw="updated",
    )
    withdrawn = make_signal(
        NOW + timedelta(hours=3),
        sunset_at=datetime(2026, 8, 1, tzinfo=UTC),
        active=False,
        raw="withdrawn",
    )

    repository.upsert_signal(first)
    repository.upsert_signal(repeated)
    repository.upsert_signal(updated)
    stored = repository.upsert_signal(withdrawn)

    assert stored.first_seen_at == NOW
    assert stored.last_seen_at == NOW + timedelta(hours=3)
    assert stored.signal == withdrawn
    assert repository.list_signals(active_only=True) == ()
    assert [change.change_type for change in repository.list_changes()] == [
        "discovered",
        "updated",
        "withdrawn",
    ]
    assert repository.list_changes()[0].previous is None
    assert repository.list_changes()[1].previous == repeated
    assert repository.list_changes()[2].current == withdrawn
    repository.close()


def test_stale_signal_observation_cannot_overwrite_newer_state(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "stale.sqlite", clock=FrozenClock(NOW))
    newer = make_signal(
        NOW + timedelta(hours=2),
        sunset_at=datetime(2026, 7, 1, tzinfo=UTC),
        raw="newer",
    )
    stale = make_signal(
        NOW + timedelta(hours=1),
        sunset_at=datetime(2027, 1, 1, tzinfo=UTC),
        raw="stale",
    )

    repository.upsert_signal(newer)
    result = repository.upsert_signal(stale)

    assert result.signal == newer
    assert result.last_seen_at == newer.observed_at
    assert repository.list_signals() == (newer,)
    assert [change.change_type for change in repository.list_changes()] == ["discovered"]
    repository.close()


def test_change_insert_failure_rolls_back_signal_update(tmp_path: Path) -> None:
    database = tmp_path / "rollback.sqlite"
    repository = SQLiteRepository(database, clock=FrozenClock(NOW))
    original = make_signal(NOW)
    repository.upsert_signal(original)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_updated_change
            BEFORE INSERT ON changes
            WHEN NEW.change_type = 'updated'
            BEGIN
                SELECT RAISE(ABORT, 'test rejection');
            END
            """
        )

    changed = make_signal(
        NOW + timedelta(hours=1),
        sunset_at=datetime(2026, 7, 1, tzinfo=UTC),
        raw="changed",
    )
    with pytest.raises(sqlite3.IntegrityError, match="test rejection"):
        repository.upsert_signal(changed)

    stored = repository.get_signal(original.signal_key)
    assert stored is not None
    assert stored.signal == original
    assert stored.last_seen_at == NOW
    assert [change.change_type for change in repository.list_changes()] == ["discovered"]
    repository.close()


def test_dependency_foreign_key_failure_leaves_existing_rows_intact(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "foreign-key.sqlite", clock=FrozenClock(NOW))
    consumer = Consumer(id="checkout", name="Checkout")
    repository.upsert_consumer(consumer)

    invalid = ConsumerDependency(
        consumer_id="missing",
        endpoint_key=ENDPOINT.key,
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        repository.upsert_dependency(invalid)

    assert repository.list_consumers() == (consumer,)
    assert repository.list_dependencies() == ()
    repository.close()


def test_http_cache_is_bounded_and_host_request_time_is_monotonic(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(
        tmp_path / "cache.sqlite",
        clock=FrozenClock(NOW),
        max_cache_entries=2,
    )

    for offset, key in enumerate(("first", "second", "third")):
        fetched = NOW + timedelta(minutes=offset)
        repository.put_http_cache(
            key,
            status_code=200,
            headers=(
                ("Sunset", "Tue, 01 Sep 2026 00:00:00 GMT"),
                ("ETag", f'"{key}"'),
            ),
            fetched_at=fetched,
            validated_at=fetched,
            expires_at=fetched + timedelta(minutes=30),
            redacted_url=f"https://api.example.test/v1?token=REDACTED&entry={key}",
        )

    assert repository.get_http_cache("first") is None
    cached = repository.get_http_cache("third")
    assert cached is not None
    assert cached.headers[0][0] == "sunset"
    assert cached.etag == '"third"'
    assert cached.redacted_url.endswith("entry=third")

    assert repository.prune_http_cache(now=NOW + timedelta(minutes=31)) == 1
    assert repository.get_http_cache("second") is None
    assert repository.get_http_cache("third") is not None

    latest = repository.set_host_last_request(
        "API.EXAMPLE.TEST.",
        requested_at=NOW + timedelta(minutes=5),
    )
    unchanged = repository.set_host_last_request(
        "api.example.test",
        requested_at=NOW,
    )
    assert latest == NOW + timedelta(minutes=5)
    assert unchanged == latest
    assert repository.get_host_last_request("api.example.test") == latest
    repository.close()


def test_invalid_persisted_json_never_escapes_as_a_domain_model(tmp_path: Path) -> None:
    database = tmp_path / "invalid.sqlite"
    repository = SQLiteRepository(database, clock=FrozenClock(NOW))
    repository.upsert_consumer(Consumer(id="checkout", name="Checkout"))

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE consumers SET payload_json = '{}' WHERE consumer_id = 'checkout'"
        )

    with pytest.raises(RepositoryDataError, match="consumer payload"):
        repository.list_consumers()
    repository.close()
