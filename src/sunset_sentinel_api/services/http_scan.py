"""Application service for turning safe HTTP metadata into lifecycle evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sunset_sentinel_api.adapters.http_client import (
    FetchResult,
    HttpLifecycleClient,
    redact_query_values,
)
from sunset_sentinel_api.adapters.sqlite_repository import (
    SQLiteRepository,
    StoredLifecycleSignal,
)
from sunset_sentinel_api.domain.enums import (
    HeaderMode,
    ParseStatus,
    ScopeKind,
    SignalCompliance,
    SignalSource,
)
from sunset_sentinel_api.domain.headers import parse_lifecycle_headers
from sunset_sentinel_api.domain.models import (
    Diagnostic,
    EndpointRef,
    LifecycleSignal,
    as_utc,
    validate_http_url,
)


@dataclass(frozen=True, slots=True)
class HttpScanTarget:
    """One allowlisted HTTP endpoint whose lifecycle metadata should be checked."""

    target_id: str
    url: str
    method: str = "GET"
    path: str | None = None
    operation_id: str | None = None

    def endpoint(self) -> EndpointRef:
        """Build the normalized endpoint identity used by impact mapping."""

        from urllib.parse import urlsplit

        validated_url = validate_http_url(self.url, field_name="scan URL")
        parsed = urlsplit(validated_url)
        endpoint_path = self.path if self.path is not None else (parsed.path or "/")
        return EndpointRef(
            target_id=self.target_id,
            method=self.method,
            path=endpoint_path,
            operation_id=self.operation_id,
        )


@dataclass(frozen=True, slots=True)
class HttpScanOutcome:
    """Safe result of one fetch, parse, and optional persistence operation."""

    fetch: FetchResult
    parsed_signal: LifecycleSignal | None = None
    stored: StoredLifecycleSignal | None = None
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def persisted(self) -> bool:
        """Whether accepted lifecycle evidence was written to the repository."""

        return self.stored is not None


def scan_http_target(
    *,
    client: HttpLifecycleClient,
    repository: SQLiteRepository,
    target: HttpScanTarget,
    observed_at: datetime,
    mode: HeaderMode = HeaderMode.COMPAT,
) -> HttpScanOutcome:
    """Fetch one endpoint and persist accepted Sunset/Deprecation evidence."""

    now = as_utc(observed_at, field_name="observed_at")
    endpoint = target.endpoint()
    result = client.fetch(target.url)
    if not result.ok:
        return HttpScanOutcome(fetch=result)

    parsed = parse_lifecycle_headers(
        result.headers,
        now=now,
        mode=mode,
        response_url=result.url,
    )
    if not parsed.has_signal:
        statuses = {parsed.deprecation.status, parsed.sunset.status}
        if statuses == {ParseStatus.ABSENT}:
            signal_key = _signal_key(
                target_id=target.target_id,
                endpoint_key=endpoint.key,
                safe_url=result.url,
            )
            existing = repository.get_signal(signal_key)
            if existing is not None and existing.signal.active:
                withdrawn = existing.signal.model_copy(
                    update={
                        "active": False,
                        "observed_at": now,
                        "raw_sha256": _header_digest(result.headers),
                        "diagnostics": parsed.diagnostics,
                    }
                )
                stored = repository.upsert_signal(withdrawn)
                return HttpScanOutcome(
                    fetch=result,
                    parsed_signal=withdrawn,
                    stored=stored,
                    diagnostics=parsed.diagnostics,
                )
        return HttpScanOutcome(fetch=result, diagnostics=parsed.diagnostics)

    compliance = (
        SignalCompliance.LEGACY
        if ParseStatus.LEGACY in {parsed.deprecation.status, parsed.sunset.status}
        else SignalCompliance.RFC
    )
    signal = LifecycleSignal(
        signal_key=_signal_key(
            target_id=target.target_id,
            endpoint_key=endpoint.key,
            safe_url=result.url,
        ),
        target_id=target.target_id,
        source=SignalSource.HTTP_HEADER,
        source_ref=result.url,
        scope=ScopeKind.ENDPOINT,
        endpoint=endpoint,
        deprecated=parsed.deprecation.deprecated,
        deprecation_at=parsed.deprecation.value,
        sunset_at=parsed.sunset.value,
        documentation_url=(
            redact_query_values(parsed.documentation_urls[0]) if parsed.documentation_urls else None
        ),
        observed_at=now,
        compliance=compliance,
        raw_sha256=_header_digest(result.headers),
        diagnostics=parsed.diagnostics,
    )
    stored = repository.upsert_signal(signal)
    return HttpScanOutcome(
        fetch=result,
        parsed_signal=signal,
        stored=stored,
        diagnostics=parsed.diagnostics,
    )


def _signal_key(*, target_id: str, endpoint_key: str, safe_url: str) -> str:
    material = f"http_header\0{target_id}\0{endpoint_key}\0{safe_url}"
    return f"http:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _header_digest(headers: tuple[tuple[str, str], ...]) -> str:
    canonical = json.dumps(
        sorted(headers),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "HttpScanOutcome",
    "HttpScanTarget",
    "scan_http_target",
]
