"""Pure reconciliation of lifecycle evidence into stable assessed records."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime

from pydantic import field_validator

from sunset_sentinel_api.domain.enums import ScopeKind
from sunset_sentinel_api.domain.lifecycle import determine_lifecycle_state
from sunset_sentinel_api.domain.models import (
    Consumer,
    ConsumerDependency,
    EndpointRef,
    FrozenModel,
    LifecycleRecord,
    LifecycleSignal,
    as_utc,
)
from sunset_sentinel_api.domain.scoring import score_lifecycle


class AssessedRecord(FrozenModel):
    """One lifecycle record with its normalized supporting context."""

    record: LifecycleRecord
    signals: tuple[LifecycleSignal, ...]
    documentation_urls: tuple[str, ...] = ()
    replacements: tuple[str, ...] = ()


class Assessment(FrozenModel):
    """Immutable output of one deterministic assessment run."""

    generated_at: datetime
    entries: tuple[AssessedRecord, ...]

    @field_validator("generated_at")
    @classmethod
    def normalize_generated_at(cls, value: datetime) -> datetime:
        return as_utc(value, field_name="generated_at")

    @property
    def records(self) -> tuple[LifecycleRecord, ...]:
        """Return the reconciled records without discarding entry context."""

        return tuple(entry.record for entry in self.entries)


SeenMapping = Mapping[str, datetime]
_GroupKey = tuple[str, str, str]


def assess_lifecycle(
    *,
    signals: Iterable[LifecycleSignal],
    consumers: Iterable[Consumer] = (),
    dependencies: Iterable[ConsumerDependency] = (),
    first_seen: SeenMapping | None = None,
    last_seen: SeenMapping | None = None,
    now: datetime,
) -> Assessment:
    """Group evidence, choose conservative dates, and calculate current risk."""

    normalized_now = as_utc(now, field_name="now")
    signal_groups: dict[_GroupKey, list[LifecycleSignal]] = defaultdict(list)
    for signal in signals:
        signal_groups[_group_key(signal)].append(signal)

    consumers_by_id = _deduplicate_consumers(consumers)
    all_dependencies = tuple(dependencies)
    entries: list[AssessedRecord] = []
    for group_key in sorted(signal_groups):
        grouped_signals = tuple(sorted(signal_groups[group_key], key=_signal_sort_key))
        entry = _assess_group(
            group_key=group_key,
            signals=grouped_signals,
            consumers_by_id=consumers_by_id,
            dependencies=all_dependencies,
            first_seen=first_seen,
            last_seen=last_seen,
            now=normalized_now,
        )
        entries.append(entry)

    return Assessment(generated_at=normalized_now, entries=tuple(entries))


def _assess_group(
    *,
    group_key: _GroupKey,
    signals: tuple[LifecycleSignal, ...],
    consumers_by_id: Mapping[str, Consumer],
    dependencies: tuple[ConsumerDependency, ...],
    first_seen: SeenMapping | None,
    last_seen: SeenMapping | None,
    now: datetime,
) -> AssessedRecord:
    scope_value, target_id, identity = group_key
    scope = ScopeKind(scope_value)
    active_signals = tuple(signal for signal in signals if signal.active)

    deprecation_dates = tuple(
        sorted(
            {
                signal.deprecation_at
                for signal in active_signals
                if signal.deprecation_at is not None
            }
        )
    )
    sunset_dates = tuple(
        sorted({signal.sunset_at for signal in active_signals if signal.sunset_at is not None})
    )
    effective_deprecation = deprecation_dates[0] if deprecation_dates else None
    effective_sunset = sunset_dates[0] if sunset_dates else None
    conflict = (
        len(deprecation_dates) > 1
        or len(sunset_dates) > 1
        or (
            effective_deprecation is not None
            and effective_sunset is not None
            and effective_sunset < effective_deprecation
        )
        or any(
            signal.deprecation_at is not None
            and signal.sunset_at is not None
            and signal.sunset_at < signal.deprecation_at
            for signal in active_signals
        )
    )
    deprecated = any(signal.deprecated for signal in active_signals) or (
        effective_deprecation is not None and effective_deprecation <= now
    )
    active = bool(active_signals)

    endpoints = _group_endpoints(scope=scope, signals=signals)
    relevant_dependencies = _relevant_dependencies(
        scope=scope,
        target_id=target_id,
        endpoints=endpoints,
        dependencies=dependencies,
    )
    relevant_consumer_ids = {dependency.consumer_id for dependency in relevant_dependencies}
    relevant_consumers = tuple(
        consumers_by_id[consumer_id]
        for consumer_id in sorted(relevant_consumer_ids)
        if consumer_id in consumers_by_id
    )

    state = determine_lifecycle_state(
        now=now,
        deprecated=deprecated,
        deprecation_at=effective_deprecation,
        sunset_at=effective_sunset,
        active=active,
        conflict=conflict,
    )
    score = score_lifecycle(
        now=now,
        deprecated=deprecated,
        deprecation_at=effective_deprecation,
        sunset_at=effective_sunset,
        conflict=conflict,
        endpoints=endpoints,
        consumers=relevant_consumers,
        dependencies=relevant_dependencies,
        service_scope=scope is ScopeKind.SERVICE,
    )
    first_seen_at = min(
        _mapped_seen_at(first_seen, signal=signal, fallback=signal.observed_at)
        for signal in signals
    )
    last_seen_at = max(
        _mapped_seen_at(last_seen, signal=signal, fallback=signal.observed_at) for signal in signals
    )
    record = LifecycleRecord(
        id=_record_id(scope=scope, target_id=target_id, identity=identity),
        target_id=target_id,
        scope=scope,
        endpoints=endpoints,
        consumers=relevant_consumers,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        effective_deprecation_at=effective_deprecation,
        effective_sunset_at=effective_sunset,
        deprecated=deprecated,
        active=active,
        date_conflict=conflict,
        state=state,
        score=score,
        scored_at=now,
    )
    documentation_urls = tuple(
        sorted(
            {signal.documentation_url for signal in signals if signal.documentation_url is not None}
        )
    )
    replacements = tuple(
        sorted({signal.replacement for signal in signals if signal.replacement is not None})
    )
    return AssessedRecord(
        record=record,
        signals=signals,
        documentation_urls=documentation_urls,
        replacements=replacements,
    )


def _group_key(signal: LifecycleSignal) -> _GroupKey:
    if signal.scope is ScopeKind.SERVICE:
        return (ScopeKind.SERVICE.value, signal.target_id, "service")
    if signal.endpoint is None:
        raise ValueError("endpoint-scoped signals must include an endpoint")
    return (ScopeKind.ENDPOINT.value, signal.target_id, signal.endpoint.key)


def _group_endpoints(
    *,
    scope: ScopeKind,
    signals: tuple[LifecycleSignal, ...],
) -> tuple[EndpointRef, ...]:
    if scope is ScopeKind.SERVICE:
        return ()
    endpoints = {
        signal.endpoint.key: signal.endpoint for signal in signals if signal.endpoint is not None
    }
    return tuple(endpoints[key] for key in sorted(endpoints))


def _relevant_dependencies(
    *,
    scope: ScopeKind,
    target_id: str,
    endpoints: tuple[EndpointRef, ...],
    dependencies: tuple[ConsumerDependency, ...],
) -> tuple[ConsumerDependency, ...]:
    endpoint_keys = {endpoint.key for endpoint in endpoints}
    relevant = {
        (dependency.consumer_id, dependency.endpoint_key): dependency
        for dependency in dependencies
        if (
            dependency.endpoint_key in endpoint_keys
            if scope is ScopeKind.ENDPOINT
            else _target_from_endpoint_key(dependency.endpoint_key) == target_id
        )
    }
    return tuple(relevant[key] for key in sorted(relevant))


def _target_from_endpoint_key(endpoint_key: str) -> str:
    return endpoint_key.partition("\n")[0]


def _deduplicate_consumers(consumers: Iterable[Consumer]) -> dict[str, Consumer]:
    result: dict[str, Consumer] = {}
    for consumer in sorted(consumers, key=lambda item: (item.id, item.model_dump_json())):
        result[consumer.id] = consumer
    return result


def _mapped_seen_at(
    mapping: SeenMapping | None,
    *,
    signal: LifecycleSignal,
    fallback: datetime,
) -> datetime:
    value = fallback if mapping is None else mapping.get(signal.signal_key, fallback)
    return as_utc(value, field_name=f"seen timestamp for {signal.signal_key}")


def _signal_sort_key(signal: LifecycleSignal) -> tuple[str, str, str, str, str]:
    return (
        signal.source.value,
        signal.source_ref,
        signal.signal_key,
        signal.observed_at.isoformat(),
        signal.raw_sha256,
    )


def _record_id(*, scope: ScopeKind, target_id: str, identity: str) -> str:
    if scope is ScopeKind.SERVICE:
        return f"{target_id}:service"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{target_id}:endpoint:{digest}"
