from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from sunset_sentinel_api.adapters.file_sources import SourceBatch
from sunset_sentinel_api.adapters.sqlite_repository import SQLiteRepository
from sunset_sentinel_api.domain import (
    Consumer,
    ConsumerDependency,
    Criticality,
    EndpointRef,
    LifecycleSignal,
    ScopeKind,
    SignalSource,
)
from sunset_sentinel_api.services.monitor import (
    IngestSummary,
    assess_repository,
    import_file_sources,
    ingest_batch,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ENDPOINT = EndpointRef(
    target_id="payments",
    method="GET",
    path="/v1/orders",
    operation_id="listOrders",
)


def make_signal(
    *,
    observed_at: datetime = NOW,
    sunset_at: datetime = datetime(2026, 6, 1, tzinfo=UTC),
    active: bool = True,
    raw: str = "original",
) -> LifecycleSignal:
    return LifecycleSignal(
        signal_key="manual:payments:orders-v1",
        target_id="payments",
        source=SignalSource.MANUAL,
        source_ref="manual.json#/signals/0",
        scope=ScopeKind.ENDPOINT,
        endpoint=ENDPOINT,
        deprecated=True,
        deprecation_at=datetime(2026, 2, 1, tzinfo=UTC),
        sunset_at=sunset_at,
        documentation_url="https://docs.example.test/orders-v2",
        replacement="/v2/orders",
        observed_at=observed_at,
        active=active,
        raw_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    )


def make_batch(signal: LifecycleSignal | None = None) -> SourceBatch:
    consumer = Consumer(
        id="checkout",
        name="Checkout",
        criticality=Criticality.CRITICAL,
    )
    dependency = ConsumerDependency(
        consumer_id=consumer.id,
        endpoint_key=ENDPOINT.key,
        evidence="src/orders.py:12",
    )
    return SourceBatch(
        signals=() if signal is None else (signal,),
        consumers=(consumer,),
        dependencies=(dependency,),
    )


def test_empty_batch_and_empty_repository_are_deterministic(tmp_path: Path) -> None:
    with SQLiteRepository(tmp_path / "empty.sqlite") as repository:
        summary = ingest_batch(repository, SourceBatch(), observed_at=NOW)
        assessment = assess_repository(repository, now=NOW)

    assert summary == IngestSummary(
        consumers=0,
        dependencies=0,
        signals=0,
        discovered=0,
        updated=0,
        withdrawn=0,
    )
    assert summary.changes == 0
    assert assessment.generated_at == NOW
    assert assessment.entries == ()


def test_ingest_orders_entities_and_counts_actual_signal_transitions(
    tmp_path: Path,
) -> None:
    later = NOW + timedelta(days=1)
    updated_at = NOW + timedelta(days=2)
    withdrawn_at = NOW + timedelta(days=3)
    with SQLiteRepository(tmp_path / "ingest.sqlite") as repository:
        discovered = ingest_batch(
            repository,
            make_batch(make_signal(observed_at=NOW - timedelta(days=30))),
            observed_at=NOW,
        )
        repeated = ingest_batch(
            repository,
            make_batch(make_signal(observed_at=NOW - timedelta(days=30))),
            observed_at=later,
        )
        updated = ingest_batch(
            repository,
            make_batch(
                make_signal(
                    observed_at=NOW,
                    sunset_at=datetime(2026, 5, 1, tzinfo=UTC),
                    raw="updated",
                )
            ),
            observed_at=updated_at,
        )
        withdrawn = ingest_batch(
            repository,
            make_batch(
                make_signal(
                    observed_at=NOW,
                    sunset_at=datetime(2026, 5, 1, tzinfo=UTC),
                    active=False,
                    raw="withdrawn",
                )
            ),
            observed_at=withdrawn_at,
        )
        stored = repository.get_signal("manual:payments:orders-v1")

    assert discovered == IngestSummary(
        consumers=1,
        dependencies=1,
        signals=1,
        discovered=1,
        updated=0,
        withdrawn=0,
    )
    assert repeated.discovered == repeated.updated == repeated.withdrawn == 0
    assert repeated.signals == 1
    assert updated.updated == 1
    assert withdrawn.withdrawn == 1
    assert stored is not None
    assert stored.first_seen_at == NOW
    assert stored.last_seen_at == withdrawn_at
    assert stored.signal.observed_at == withdrawn_at


def test_assessment_receives_persisted_first_and_last_seen_maps(
    tmp_path: Path,
) -> None:
    later = NOW + timedelta(days=4)
    batch = make_batch(make_signal())
    with SQLiteRepository(tmp_path / "assessment.sqlite") as repository:
        ingest_batch(repository, batch, observed_at=NOW)
        ingest_batch(repository, batch, observed_at=later)

        assessment = assess_repository(repository, now=later)

    assert len(assessment.entries) == 1
    record = assessment.records[0]
    assert record.first_seen_at == NOW
    assert record.last_seen_at == later
    assert record.consumers[0].id == "checkout"


def test_import_file_sources_loads_all_file_kinds_and_is_repeatable(
    tmp_path: Path,
) -> None:
    openapi_path = tmp_path / "payments.openapi.json"
    manual_path = tmp_path / "manual.json"
    consumers_path = tmp_path / "consumers.json"
    openapi_path.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "Payments", "version": "1.0.0"},
                "paths": {
                    "/v1/orders": {
                        "get": {
                            "operationId": "listOrders",
                            "deprecated": True,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manual_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "license": "MIT",
                "signals": [
                    {
                        "signal_key": "payments-service",
                        "target_id": "payments",
                        "scope": "service",
                        "sunset_at": "2026-12-01T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    consumers_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "license": "MIT",
                "consumers": [
                    {
                        "id": "checkout",
                        "name": "Checkout",
                        "criticality": "critical",
                    }
                ],
                "dependencies": [
                    {
                        "consumer_id": "checkout",
                        "target_id": "payments",
                        "method": "GET",
                        "path": "/v1/orders",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with SQLiteRepository(tmp_path / "files.sqlite") as repository:
        first = import_file_sources(
            repository,
            observed_at=NOW,
            openapi_files={"payments": openapi_path},
            manual_feed_files=(manual_path,),
            consumer_files=(consumers_path,),
        )
        second = import_file_sources(
            repository,
            observed_at=NOW + timedelta(hours=1),
            openapi_files={"payments": openapi_path},
            manual_feed_files=(manual_path,),
            consumer_files=(consumers_path,),
        )
        assessment = assess_repository(
            repository,
            now=NOW + timedelta(hours=1),
        )

    assert first == IngestSummary(
        consumers=1,
        dependencies=1,
        signals=2,
        discovered=2,
        updated=0,
        withdrawn=0,
    )
    assert second.signals == 2
    assert second.changes == 0
    assert len(assessment.entries) == 2
    assert all(record.last_seen_at == NOW + timedelta(hours=1) for record in assessment.records)


def test_naive_ingestion_time_fails_before_any_write(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "naive.sqlite")

    with pytest.raises(ValueError, match="timezone"):
        ingest_batch(
            repository,
            make_batch(make_signal()),
            observed_at=datetime(2026, 1, 1),
        )

    assert repository.list_consumers() == ()
    assert repository.list_dependencies() == ()
    assert repository.list_signals() == ()
    repository.close()


def test_ingest_summary_is_frozen() -> None:
    summary = IngestSummary(
        consumers=0,
        dependencies=0,
        signals=0,
        discovered=0,
        updated=0,
        withdrawn=0,
    )

    with pytest.raises(ValidationError):
        summary.signals = 1
