from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sunset_sentinel_api.domain import (
    Consumer,
    ConsumerDependency,
    Criticality,
    EndpointRef,
    LifecycleSignal,
    LifecycleState,
    ScopeKind,
    SignalCompliance,
    SignalSource,
)
from sunset_sentinel_api.exporters import (
    assessment_to_json,
    build_issue_drafts,
    render_ics_calendar,
    render_markdown_report,
    render_migration_checklist,
)
from sunset_sentinel_api.services.assessment import assess_lifecycle

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_signal(
    *,
    key: str,
    target: str = "payments",
    endpoint: EndpointRef | None = None,
    scope: ScopeKind = ScopeKind.ENDPOINT,
    source: SignalSource = SignalSource.MANUAL,
    deprecation_at: datetime | None = None,
    sunset_at: datetime | None = None,
    deprecated: bool = False,
    active: bool = True,
    documentation_url: str | None = None,
    replacement: str | None = None,
    raw: str = "a",
) -> LifecycleSignal:
    return LifecycleSignal(
        signal_key=key,
        target_id=target,
        source=source,
        source_ref=f"private/source/{key}?token=source-secret",
        scope=scope,
        endpoint=endpoint,
        deprecated=deprecated,
        deprecation_at=deprecation_at,
        sunset_at=sunset_at,
        documentation_url=documentation_url,
        replacement=replacement,
        observed_at=NOW,
        active=active,
        compliance=SignalCompliance.RFC,
        raw_sha256=raw * 64,
    )


def test_assessment_groups_endpoint_signals_and_uses_earliest_dates() -> None:
    endpoint = EndpointRef(target_id="payments", method="GET", path="/v1/orders")
    signals = [
        make_signal(
            key="later",
            endpoint=endpoint,
            deprecation_at=NOW + timedelta(days=60),
            sunset_at=NOW + timedelta(days=120),
            raw="b",
        ),
        make_signal(
            key="earlier",
            endpoint=endpoint,
            deprecation_at=NOW + timedelta(days=30),
            sunset_at=NOW + timedelta(days=90),
        ),
    ]
    consumer = Consumer(
        id="checkout",
        name="Checkout",
        criticality=Criticality.CRITICAL,
    )
    dependency = ConsumerDependency(
        consumer_id=consumer.id,
        endpoint_key=endpoint.key,
        evidence="private/repository/path.py:42",
    )

    assessment = assess_lifecycle(
        signals=reversed(signals),
        consumers=[consumer],
        dependencies=[dependency],
        first_seen={"later": NOW - timedelta(days=10)},
        last_seen={"earlier": NOW + timedelta(hours=1)},
        now=NOW,
    )
    entry = assessment.entries[0]

    assert len(assessment.entries) == 1
    assert entry.record.effective_deprecation_at == NOW + timedelta(days=30)
    assert entry.record.effective_sunset_at == NOW + timedelta(days=90)
    assert entry.record.date_conflict is True
    assert entry.record.state is LifecycleState.CONFLICTED
    assert entry.record.first_seen_at == NOW - timedelta(days=10)
    assert entry.record.last_seen_at == NOW + timedelta(hours=1)
    assert entry.record.consumers == (consumer,)
    assert entry.record.score.urgency == 95


def test_assessment_separates_service_and_endpoint_scope() -> None:
    endpoint = EndpointRef(target_id="payments", method="GET", path="/v1/orders")
    endpoint_signal = make_signal(
        key="endpoint",
        endpoint=endpoint,
        deprecated=True,
    )
    service_signal = make_signal(
        key="service",
        endpoint=None,
        scope=ScopeKind.SERVICE,
        sunset_at=NOW + timedelta(days=30),
    )

    assessment = assess_lifecycle(
        signals=[service_signal, endpoint_signal],
        now=NOW,
    )

    assert len(assessment.records) == 2
    assert {record.scope for record in assessment.records} == {
        ScopeKind.ENDPOINT,
        ScopeKind.SERVICE,
    }
    service_record = next(
        record for record in assessment.records if record.scope is ScopeKind.SERVICE
    )
    assert service_record.endpoints == ()
    assert service_record.state is LifecycleState.SUNSET_SCHEDULED


def test_all_inactive_signals_produce_withdrawn_record() -> None:
    endpoint = EndpointRef(target_id="payments", method="GET", path="/v1/orders")
    signal = make_signal(
        key="withdrawn",
        endpoint=endpoint,
        sunset_at=NOW - timedelta(days=1),
        active=False,
    )

    record = assess_lifecycle(signals=[signal], now=NOW).records[0]

    assert record.active is False
    assert record.state is LifecycleState.WITHDRAWN
    assert record.effective_sunset_at is None
    assert record.score.urgency == 0


def test_service_scope_includes_consumers_linked_to_target_endpoints() -> None:
    endpoint = EndpointRef(target_id="payments", method="GET", path="/v1/orders")
    unrelated = EndpointRef(target_id="catalog", method="GET", path="/v1/items")
    consumers = [
        Consumer(id="checkout", name="Checkout"),
        Consumer(id="search", name="Search"),
    ]
    dependencies = [
        ConsumerDependency(consumer_id="checkout", endpoint_key=endpoint.key),
        ConsumerDependency(consumer_id="search", endpoint_key=unrelated.key),
    ]
    signal = make_signal(
        key="service",
        endpoint=None,
        scope=ScopeKind.SERVICE,
        sunset_at=NOW + timedelta(days=180),
    )

    record = assess_lifecycle(
        signals=[signal],
        consumers=consumers,
        dependencies=dependencies,
        now=NOW,
    ).records[0]

    assert [consumer.id for consumer in record.consumers] == ["checkout"]
    assert record.score.blast_radius == 17  # medium consumer 5 + edge 2 + service 10


def test_json_is_stable_and_excludes_raw_evidence_and_secrets() -> None:
    endpoint = EndpointRef(
        target_id="payments",
        method="GET",
        path="/v1/orders",
        operation_id="listOrders",
    )
    signal = make_signal(
        key="scheduled",
        endpoint=endpoint,
        sunset_at=NOW + timedelta(days=30),
        documentation_url="https://docs.example.test/migrate?token=doc-secret",
        replacement="https://api.example.test/v2?api_key=replacement-secret",
    )
    assessment = assess_lifecycle(signals=[signal], now=NOW)

    first = assessment_to_json(assessment)
    second = assessment_to_json(assessment)
    payload = json.loads(first)

    assert first == second
    assert payload["records"][0]["signals"] == [
        {"active": True, "compliance": "rfc", "source": "manual"}
    ]
    assert "a" * 64 not in first
    assert "private/source" not in first
    assert "private/repository" not in first
    assert "doc-secret" not in first
    assert "replacement-secret" not in first
    assert "token=REDACTED" in first
    assert "api_key=REDACTED" in first


def test_markdown_report_contains_real_scores_and_context() -> None:
    endpoint = EndpointRef(target_id="payments", method="GET", path="/v1/orders")
    signal = make_signal(
        key="scheduled",
        endpoint=endpoint,
        sunset_at=NOW + timedelta(days=7),
        documentation_url="https://docs.example.test/migrate",
        replacement="POST /v2/orders",
    )
    report = render_markdown_report(assess_lifecycle(signals=[signal], now=NOW))

    assert "payments GET /v1/orders" in report
    assert "urgency **95**" in report
    assert "<https://docs.example.test/migrate>" in report
    assert "POST /v2/orders" in report


def test_ics_is_rfc5545_folded_stable_and_omits_unknown_dates() -> None:
    long_path = "/v1/" + "订单" * 40
    dated_endpoint = EndpointRef(target_id="payments", method="GET", path=long_path)
    unknown_endpoint = EndpointRef(target_id="payments", method="POST", path="/v1/unknown")
    dated = make_signal(
        key="dated",
        endpoint=dated_endpoint,
        deprecation_at=NOW + timedelta(days=30),
        sunset_at=NOW + timedelta(days=90),
        replacement="Use v2, then remove; old path",
    )
    unknown = make_signal(
        key="unknown",
        endpoint=unknown_endpoint,
        deprecated=True,
        raw="b",
    )
    assessment = assess_lifecycle(signals=[unknown, dated], now=NOW)

    first = render_ics_calendar(assessment)
    second = render_ics_calendar(assessment)
    physical_lines = first.removesuffix("\r\n").split("\r\n")

    assert first == second
    assert "\n" not in first.replace("\r\n", "")
    assert first.count("BEGIN:VEVENT") == 2
    assert first.count("STATUS:TENTATIVE") == 2
    assert "DTSTART:20260131T000000Z" in first
    assert "DTSTART:20260401T000000Z" in first
    assert "POST /v1/unknown" not in first
    assert "\\," in first and "\\;" in first
    assert any(line.startswith(" ") for line in physical_lines)
    assert all(len(line.encode("utf-8")) <= 75 for line in physical_lines)
    assert all(not line.endswith((" ", "\t")) for line in physical_lines)


def test_ics_uses_stable_distinct_uids_and_skips_withdrawn() -> None:
    endpoint = EndpointRef(target_id="payments", method="GET", path="/v1/orders")
    active = make_signal(
        key="active",
        endpoint=endpoint,
        deprecation_at=NOW + timedelta(days=1),
        sunset_at=NOW + timedelta(days=2),
    )
    withdrawn_endpoint = EndpointRef(
        target_id="payments",
        method="POST",
        path="/v1/withdrawn",
    )
    withdrawn = make_signal(
        key="withdrawn",
        endpoint=withdrawn_endpoint,
        sunset_at=NOW + timedelta(days=3),
        active=False,
        raw="b",
    )

    calendar = render_ics_calendar(assess_lifecycle(signals=[active, withdrawn], now=NOW))
    uids = [line for line in calendar.split("\r\n") if line.startswith("UID:")]

    assert len(uids) == 2
    assert len(set(uids)) == 2
    assert all(uid.endswith("@sunset-sentinel") for uid in uids)
    assert "withdrawn" not in calendar


def test_issue_drafts_cover_conflict_and_do_not_include_raw_evidence() -> None:
    endpoint = EndpointRef(target_id="payments", method="GET", path="/v1/orders")
    signals = [
        make_signal(
            key="one",
            endpoint=endpoint,
            deprecation_at=NOW + timedelta(days=60),
            replacement=None,
        ),
        make_signal(
            key="two",
            endpoint=endpoint,
            deprecation_at=NOW + timedelta(days=30),
            raw="b",
        ),
    ]

    drafts = build_issue_drafts(assess_lifecycle(signals=signals, now=NOW))

    assert len(drafts) == 1
    assert "needs-clarification" in drafts[0].labels
    assert "clarify conflicting lifecycle dates" in drafts[0].body
    assert "Select and document a replacement API" in drafts[0].body
    assert "a" * 64 not in drafts[0].body
    assert "private/source" not in drafts[0].body


def test_checklist_has_stable_task_ids_and_replacement_branch() -> None:
    endpoint = EndpointRef(target_id="payments", method="GET", path="/v1/orders")
    signal = make_signal(
        key="scheduled",
        endpoint=endpoint,
        deprecated=True,
        replacement="GET /v2/orders",
    )
    assessment = assess_lifecycle(signals=[signal], now=NOW)

    first = render_migration_checklist(assessment)
    second = render_migration_checklist(assessment)

    assert first == second
    assert "<!-- ss:assign-owner:" in first
    assert "Confirm the documented replacement API" in first
    assert "Select and document a replacement API" not in first
    assert "Remove the deprecated dependency and obsolete credentials" in first
