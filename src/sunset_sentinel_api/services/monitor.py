"""Deterministic orchestration for source ingestion and repository assessment."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import Field

from sunset_sentinel_api.adapters.file_sources import SourceBatch, load_file_sources
from sunset_sentinel_api.adapters.sqlite_repository import SQLiteRepository
from sunset_sentinel_api.domain.models import FrozenModel, as_utc
from sunset_sentinel_api.services.assessment import Assessment, assess_lifecycle


class IngestSummary(FrozenModel):
    """Immutable counts from one completed ingestion batch."""

    consumers: int = Field(ge=0)
    dependencies: int = Field(ge=0)
    signals: int = Field(ge=0)
    discovered: int = Field(ge=0)
    updated: int = Field(ge=0)
    withdrawn: int = Field(ge=0)

    @property
    def changes(self) -> int:
        """Return the number of material lifecycle transitions."""

        return self.discovered + self.updated + self.withdrawn


def ingest_batch(
    repository: SQLiteRepository,
    batch: SourceBatch,
    *,
    observed_at: datetime,
) -> IngestSummary:
    """Persist a normalized batch in foreign-key-safe deterministic order."""

    normalized_observed_at = as_utc(observed_at, field_name="observed_at")
    changes_before = len(repository.list_changes())

    for consumer in batch.consumers:
        repository.upsert_consumer(consumer, observed_at=normalized_observed_at)
    for dependency in batch.dependencies:
        repository.upsert_dependency(
            dependency,
            observed_at=normalized_observed_at,
        )
    for signal in batch.signals:
        normalized_signal = signal.model_copy(update={"observed_at": normalized_observed_at})
        repository.upsert_signal(normalized_signal)

    new_changes = repository.list_changes()[changes_before:]
    change_counts = Counter(change.change_type for change in new_changes)
    return IngestSummary(
        consumers=len(batch.consumers),
        dependencies=len(batch.dependencies),
        signals=len(batch.signals),
        discovered=change_counts["discovered"],
        updated=change_counts["updated"],
        withdrawn=change_counts["withdrawn"],
    )


def assess_repository(
    repository: SQLiteRepository,
    *,
    now: datetime,
) -> Assessment:
    """Assess the current repository using its durable seen-time intervals."""

    normalized_now = as_utc(now, field_name="now")
    records = repository.list_signal_records()
    return assess_lifecycle(
        signals=(record.signal for record in records),
        consumers=repository.list_consumers(),
        dependencies=repository.list_dependencies(),
        first_seen={record.signal_key: record.first_seen_at for record in records},
        last_seen={record.signal_key: record.last_seen_at for record in records},
        now=normalized_now,
    )


def import_file_sources(
    repository: SQLiteRepository,
    *,
    observed_at: datetime,
    openapi_files: Mapping[str, str | Path] | None = None,
    manual_feed_files: Sequence[str | Path] = (),
    consumer_files: Sequence[str | Path] = (),
) -> IngestSummary:
    """Load local files through strict adapters and persist the merged batch."""

    normalized_observed_at = as_utc(observed_at, field_name="observed_at")
    batch = load_file_sources(
        observed_at=normalized_observed_at,
        openapi_files=openapi_files,
        manual_feed_files=manual_feed_files,
        consumer_files=consumer_files,
    )
    return ingest_batch(
        repository,
        batch,
        observed_at=normalized_observed_at,
    )


__all__ = [
    "IngestSummary",
    "assess_repository",
    "import_file_sources",
    "ingest_batch",
]
