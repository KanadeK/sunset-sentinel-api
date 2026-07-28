"""Deterministic, privacy-conscious exports for lifecycle assessments."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from urllib.parse import SplitResult, urlsplit, urlunsplit

from sunset_sentinel_api.domain.enums import LifecycleState, ScopeKind
from sunset_sentinel_api.domain.models import FrozenModel, LifecycleRecord, as_utc
from sunset_sentinel_api.services.assessment import AssessedRecord, Assessment


class IssueDraft(FrozenModel):
    """A GitHub-compatible issue draft without any external side effect."""

    record_id: str
    title: str
    labels: tuple[str, ...]
    body: str


def assessment_to_json(assessment: Assessment) -> str:
    """Serialize a stable safe projection, excluding raw evidence and diagnostics."""

    payload = {
        "generated_at": _iso_datetime(assessment.generated_at),
        "records": [_entry_payload(entry) for entry in _sorted_entries(assessment)],
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render_markdown_report(assessment: Assessment) -> str:
    """Render a human-readable assessment report with stable ordering."""

    lines = [
        "# Sunset Sentinel lifecycle report",
        "",
        f"Generated: `{_iso_datetime(assessment.generated_at)}`",
        "",
        f"Records: **{len(assessment.entries)}**",
    ]
    for entry in _sorted_entries(assessment):
        record = entry.record
        lines.extend(
            [
                "",
                f"## {_md_escape(_record_display(record))}",
                "",
                f"- State: `{record.state.value}`",
                f"- Scope: `{record.scope.value}`",
                f"- Deprecation: `{_display_datetime(record.effective_deprecation_at)}`",
                f"- Sunset: `{_display_datetime(record.effective_sunset_at)}`",
                (
                    f"- Scores: urgency **{record.score.urgency}**, "
                    f"blast radius **{record.score.blast_radius}**, "
                    f"priority **{record.score.priority}**"
                ),
                f"- Date conflict: `{'yes' if record.date_conflict else 'no'}`",
                f"- Signal sources: {_signal_sources(entry)}",
                f"- Consumers: {_consumer_list(record)}",
                f"- Documentation: {_link_list(entry.documentation_urls)}",
                f"- Replacements: {_replacement_list(entry.replacements)}",
            ]
        )
    return "\n".join(lines) + "\n"


def render_ics_calendar(assessment: Assessment) -> str:
    """Render RFC 5545 calendar events for known lifecycle dates only."""

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Sunset Sentinel API//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    events: list[tuple[datetime, str, str, AssessedRecord]] = []
    for entry in _sorted_entries(assessment):
        if entry.record.state is LifecycleState.WITHDRAWN:
            continue
        if entry.record.effective_deprecation_at is not None:
            events.append(
                (
                    entry.record.effective_deprecation_at,
                    entry.record.id,
                    "deprecation",
                    entry,
                )
            )
        if entry.record.effective_sunset_at is not None:
            events.append(
                (
                    entry.record.effective_sunset_at,
                    entry.record.id,
                    "sunset",
                    entry,
                )
            )

    for date, _record_id, milestone, entry in sorted(events, key=lambda event: event[:3]):
        record = entry.record
        summary = f"{milestone.title()}: {_record_display(record)}"
        description_parts = [
            f"State: {record.state.value}",
            f"Urgency: {record.score.urgency}/100",
            f"Consumers: {_consumer_plain_list(record)}",
        ]
        if entry.documentation_urls:
            description_parts.append(f"Documentation: {_safe_url(entry.documentation_urls[0])}")
        if entry.replacements:
            description_parts.append(f"Replacement: {_safe_text(entry.replacements[0])}")
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{_event_uid(record.id, milestone)}",
                f"DTSTAMP:{_ics_datetime(assessment.generated_at)}",
                f"DTSTART:{_ics_datetime(date)}",
                f"SUMMARY:{_ics_escape(summary)}",
                f"DESCRIPTION:{_ics_escape('; '.join(description_parts))}",
                "STATUS:TENTATIVE",
                "TRANSP:TRANSPARENT",
                f"CATEGORIES:API,{milestone.upper()}",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold_ics_line(line) for line in lines) + "\r\n"


def build_issue_drafts(assessment: Assessment) -> tuple[IssueDraft, ...]:
    """Create one deterministic Markdown issue draft per assessed record."""

    drafts: list[IssueDraft] = []
    for entry in _sorted_entries(assessment):
        record = entry.record
        labels = ["api-lifecycle", f"priority:{record.score.priority_band.value}"]
        if record.date_conflict:
            labels.append("needs-clarification")
        if record.state is LifecycleState.WITHDRAWN:
            labels.append("withdrawn")
        title = _truncate(
            f"[Sunset Sentinel] {_record_display(record)} — {record.state.value}",
            240,
        )
        body_lines = [
            f"## {_md_escape(_record_display(record))}",
            "",
            "### Lifecycle",
            "",
            f"- State: `{record.state.value}`",
            f"- Deprecation: `{_display_datetime(record.effective_deprecation_at)}`",
            f"- Sunset: `{_display_datetime(record.effective_sunset_at)}`",
            f"- Priority: **{record.score.priority}/100**",
            f"- Date conflict: `{'yes' if record.date_conflict else 'no'}`",
            f"- Consumers: {_consumer_list(record)}",
            f"- Documentation: {_link_list(entry.documentation_urls)}",
            f"- Replacements: {_replacement_list(entry.replacements)}",
            f"- Signal sources: {_signal_sources(entry)}",
            "",
            "### Migration checklist",
            "",
            *_checklist_lines(entry),
        ]
        drafts.append(
            IssueDraft(
                record_id=record.id,
                title=title,
                labels=tuple(sorted(labels)),
                body="\n".join(body_lines) + "\n",
            )
        )
    return tuple(drafts)


def render_migration_checklist(assessment: Assessment) -> str:
    """Render a stable cross-record migration checklist."""

    lines = [
        "# API migration checklist",
        "",
        f"Generated: `{_iso_datetime(assessment.generated_at)}`",
    ]
    for entry in _sorted_entries(assessment):
        lines.extend(
            [
                "",
                f"## {_md_escape(_record_display(entry.record))}",
                "",
                *_checklist_lines(entry),
            ]
        )
    return "\n".join(lines) + "\n"


def _entry_payload(entry: AssessedRecord) -> dict[str, object]:
    record = entry.record
    return {
        "id": record.id,
        "target_id": record.target_id,
        "scope": record.scope.value,
        "state": record.state.value,
        "active": record.active,
        "deprecated": record.deprecated,
        "date_conflict": record.date_conflict,
        "first_seen_at": _iso_datetime(record.first_seen_at),
        "last_seen_at": _iso_datetime(record.last_seen_at),
        "deprecation_at": _optional_iso_datetime(record.effective_deprecation_at),
        "sunset_at": _optional_iso_datetime(record.effective_sunset_at),
        "scores": {
            "urgency": record.score.urgency,
            "urgency_band": record.score.urgency_band.value,
            "blast_radius": record.score.blast_radius,
            "blast_radius_band": record.score.blast_radius_band.value,
            "priority": record.score.priority,
            "priority_band": record.score.priority_band.value,
        },
        "endpoints": [
            {
                "method": endpoint.method,
                "path": endpoint.path,
                "operation_id": endpoint.operation_id,
            }
            for endpoint in sorted(
                record.endpoints,
                key=lambda item: (item.method, item.path, item.operation_id or ""),
            )
        ],
        "consumers": [
            {
                "id": consumer.id,
                "name": consumer.name,
                "criticality": consumer.criticality.value,
            }
            for consumer in sorted(record.consumers, key=lambda item: item.id)
        ],
        "signals": [
            {
                "source": signal.source.value,
                "compliance": signal.compliance.value,
                "active": signal.active,
            }
            for signal in entry.signals
        ],
        "documentation_urls": [_safe_url(url) for url in entry.documentation_urls],
        "replacements": [_safe_text(replacement) for replacement in entry.replacements],
    }


def _sorted_entries(assessment: Assessment) -> tuple[AssessedRecord, ...]:
    return tuple(sorted(assessment.entries, key=lambda entry: entry.record.id))


def _record_display(record: LifecycleRecord) -> str:
    if record.scope is ScopeKind.SERVICE:
        return f"{record.target_id} (service)"
    endpoint = record.endpoints[0]
    return f"{record.target_id} {endpoint.method} {endpoint.path}"


def _signal_sources(entry: AssessedRecord) -> str:
    sources = sorted({signal.source.value for signal in entry.signals})
    return ", ".join(f"`{source}`" for source in sources) or "_none_"


def _consumer_plain_list(record: LifecycleRecord) -> str:
    return ", ".join(consumer.name for consumer in record.consumers) or "none"


def _consumer_list(record: LifecycleRecord) -> str:
    values = [
        f"{_md_escape(consumer.name)} (`{consumer.criticality.value}`)"
        for consumer in record.consumers
    ]
    return ", ".join(values) or "_none_"


def _link_list(urls: tuple[str, ...]) -> str:
    values = [_safe_url(url) for url in urls]
    return ", ".join(f"<{value}>" for value in values) or "_none_"


def _replacement_list(replacements: tuple[str, ...]) -> str:
    values = [_md_escape(_safe_text(replacement)) for replacement in replacements]
    return ", ".join(values) or "_not specified_"


def _checklist_lines(entry: AssessedRecord) -> list[str]:
    record = entry.record
    tasks: list[tuple[str, str]] = []
    if record.state is LifecycleState.WITHDRAWN:
        tasks.append(("confirm-withdrawal", "Confirm the lifecycle notice was withdrawn."))
    if record.date_conflict:
        tasks.append(
            (
                "clarify-dates",
                "Ask the provider to clarify conflicting lifecycle dates.",
            )
        )
    tasks.extend(
        [
            ("assign-owner", "Assign a migration owner."),
            ("verify-docs", "Verify the provider documentation and lifecycle dates."),
            ("inventory", "Confirm all affected local consumers."),
        ]
    )
    if entry.replacements:
        tasks.append(("confirm-replacement", "Confirm the documented replacement API."))
    else:
        tasks.append(("select-replacement", "Select and document a replacement API."))
    tasks.extend(
        [
            ("contract-tests", "Add or update contract and regression tests."),
            ("stage", "Validate the migration in staging."),
            ("production", "Deploy the migration to production."),
            ("monitor", "Monitor errors, latency, and fallback usage."),
            ("remove-old", "Remove the deprecated dependency and obsolete credentials."),
        ]
    )
    return [
        f"- [ ] <!-- ss:{_task_id(record.id, slug)} --> {description}"
        for slug, description in tasks
    ]


def _task_id(record_id: str, slug: str) -> str:
    digest = hashlib.sha256(f"{record_id}:{slug}".encode()).hexdigest()[:12]
    return f"{slug}:{digest}"


def _event_uid(record_id: str, milestone: str) -> str:
    digest = hashlib.sha256(f"{record_id}:{milestone}".encode()).hexdigest()[:40]
    return f"{digest}@sunset-sentinel"


def _ics_datetime(value: datetime) -> str:
    return as_utc(value).strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _fold_ics_line(line: str) -> str:
    folded: list[str] = []
    current = ""
    current_octets = 0
    for character in line:
        encoded_length = len(character.encode("utf-8"))
        if current and current_octets + encoded_length > 75:
            if current.endswith(" "):
                folded.append(current[:-1])
                current = f"  {character}"
                current_octets = 2 + encoded_length
            else:
                folded.append(current)
                current = f" {character}"
                current_octets = 1 + encoded_length
        else:
            current += character
            current_octets += encoded_length
    folded.append(current)
    return "\r\n".join(folded)


def _safe_url(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname
    if host is None:
        return "[invalid URL]"
    netloc = f"[{host}]" if ":" in host else host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    query = ""
    if parsed.query:
        query = "&".join(
            f"{component.partition('=')[0]}=REDACTED" for component in parsed.query.split("&")
        )
    return urlunsplit(
        SplitResult(
            scheme=parsed.scheme.casefold(),
            netloc=netloc,
            path=parsed.path,
            query=query,
            fragment=parsed.fragment,
        )
    )


def _safe_text(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.casefold() in {"http", "https"} and parsed.hostname is not None:
        return _safe_url(value)
    return value.replace("\r", " ").replace("\n", " ")


def _md_escape(value: str) -> str:
    result = value.replace("\\", "\\\\").replace("\r", " ").replace("\n", " ")
    for character in ("`", "*", "_", "[", "]", "<", ">", "#", "|"):
        result = result.replace(character, f"\\{character}")
    return result


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def _iso_datetime(value: datetime) -> str:
    return as_utc(value).isoformat().replace("+00:00", "Z")


def _optional_iso_datetime(value: datetime | None) -> str | None:
    return None if value is None else _iso_datetime(value)


def _display_datetime(value: datetime | None) -> str:
    return "unknown" if value is None else _iso_datetime(value)
