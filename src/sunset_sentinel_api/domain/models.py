"""Validated, immutable models for API lifecycle evidence."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sunset_sentinel_api.domain.enums import (
    Band,
    Criticality,
    DiagnosticSeverity,
    LifecycleState,
    ParseStatus,
    ScopeKind,
    SignalCompliance,
    SignalSource,
)

_METHOD_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def as_utc(value: datetime, *, field_name: str = "datetime") -> datetime:
    """Return a second-precision UTC datetime, rejecting naive values."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(UTC).replace(microsecond=0)


def validate_http_url(value: str, *, field_name: str = "URL") -> str:
    """Validate an absolute, credential-free HTTP(S) URL."""

    if _CONTROL_RE.search(value):
        raise ValueError(f"{field_name} contains control characters")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not contain credentials")
    return value


class FrozenModel(BaseModel):
    """Strict base model for values crossing the domain boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Diagnostic(FrozenModel):
    """A safe diagnostic that references raw evidence only by digest."""

    code: str = Field(min_length=1, max_length=96)
    severity: DiagnosticSeverity
    field: str = Field(min_length=1, max_length=96)
    message: str = Field(min_length=1, max_length=512)
    raw_sha256: str | None = None

    @field_validator("raw_sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_RE.fullmatch(value):
            raise ValueError("raw_sha256 must be a lowercase SHA-256 hex digest")
        return value


class EndpointRef(FrozenModel):
    """A normalized endpoint in one monitored target."""

    target_id: str
    method: str
    path: str = Field(min_length=1, max_length=2048)
    operation_id: str | None = Field(default=None, max_length=256)

    @field_validator("target_id")
    @classmethod
    def validate_target_id(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("target_id contains unsupported characters")
        return value

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        normalized = value.upper()
        if not _METHOD_RE.fullmatch(normalized):
            raise ValueError("method must be an ASCII HTTP method token")
        return normalized

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path must start with '/'")
        if "?" in value or "#" in value:
            raise ValueError("path must not include query or fragment components")
        if _CONTROL_RE.search(value):
            raise ValueError("path contains control characters")
        return value

    @property
    def key(self) -> str:
        """Return the stable identity used for deduplication and scoring."""

        return f"{self.target_id}\n{self.method}\n{self.path}"


class Consumer(FrozenModel):
    """A local system that depends on monitored endpoints."""

    id: str
    name: str = Field(min_length=1, max_length=256)
    criticality: Criticality = Criticality.MEDIUM
    owner: str | None = Field(default=None, max_length=256)
    repository_path: str | None = Field(default=None, max_length=2048)
    tags: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("consumer id contains unsupported characters")
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(value)))
        if any(not tag or len(tag) > 64 or _CONTROL_RE.search(tag) for tag in normalized):
            raise ValueError(
                "tags must be non-empty, control-free strings of at most 64 characters"
            )
        return normalized


class ConsumerDependency(FrozenModel):
    """One local consumer-to-endpoint dependency edge."""

    consumer_id: str
    endpoint_key: str = Field(min_length=1, max_length=4096)
    evidence: str | None = Field(default=None, max_length=2048)

    @field_validator("consumer_id")
    @classmethod
    def validate_consumer_id(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("consumer_id contains unsupported characters")
        return value


class ParsedHeaderValue(FrozenModel):
    """Parsed value for one singleton lifecycle header."""

    status: ParseStatus
    value: datetime | None = None
    deprecated: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: datetime | None) -> datetime | None:
        return None if value is None else as_utc(value, field_name="header value")

    @model_validator(mode="after")
    def validate_outcome(self) -> ParsedHeaderValue:
        if self.status in {
            ParseStatus.ABSENT,
            ParseStatus.INVALID,
        } and (self.value is not None or self.deprecated):
            raise ValueError("absent or invalid headers cannot carry a parsed signal")
        if self.status is ParseStatus.VALID and self.value is None:
            raise ValueError("a standards-compliant lifecycle header must carry a date")
        if self.status is ParseStatus.LEGACY and self.value is None and not self.deprecated:
            raise ValueError("a legacy header must carry a date or a deprecated flag")
        return self


class ParsedLifecycleHeaders(FrozenModel):
    """Result of parsing lifecycle fields from one HTTP response."""

    sunset: ParsedHeaderValue
    deprecation: ParsedHeaderValue
    documentation_urls: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    @field_validator("documentation_urls")
    @classmethod
    def validate_documentation_urls(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for value in values:
            validated = validate_http_url(value, field_name="documentation URL")
            if validated not in result:
                result.append(validated)
        return tuple(result)

    @property
    def has_signal(self) -> bool:
        """Whether at least one valid or legacy lifecycle field was found."""

        accepted = {ParseStatus.VALID, ParseStatus.LEGACY}
        return self.sunset.status in accepted or self.deprecation.status in accepted


class ParsedLinks(FrozenModel):
    """Documentation links extracted from one or more Link fields."""

    urls: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for value in values:
            validated = validate_http_url(value, field_name="Link target")
            if validated not in result:
                result.append(validated)
        return tuple(result)


class LifecycleSignal(FrozenModel):
    """Normalized evidence ready to be persisted and reconciled."""

    signal_key: str = Field(min_length=1, max_length=512)
    target_id: str
    source: SignalSource
    source_ref: str = Field(min_length=1, max_length=2048)
    scope: ScopeKind
    endpoint: EndpointRef | None = None
    deprecated: bool = False
    deprecation_at: datetime | None = None
    sunset_at: datetime | None = None
    documentation_url: str | None = None
    replacement: str | None = Field(default=None, max_length=2048)
    observed_at: datetime
    active: bool = True
    compliance: SignalCompliance = SignalCompliance.RFC
    raw_sha256: str
    diagnostics: tuple[Diagnostic, ...] = ()

    @field_validator("target_id")
    @classmethod
    def validate_target_id(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("target_id contains unsupported characters")
        return value

    @field_validator("deprecation_at", "sunset_at")
    @classmethod
    def normalize_optional_datetime(cls, value: datetime | None) -> datetime | None:
        return None if value is None else as_utc(value)

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        return as_utc(value, field_name="observed_at")

    @field_validator("documentation_url")
    @classmethod
    def validate_documentation_url(cls, value: str | None) -> str | None:
        return None if value is None else validate_http_url(value, field_name="documentation_url")

    @field_validator("raw_sha256")
    @classmethod
    def validate_raw_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("raw_sha256 must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def validate_scope_and_signal(self) -> LifecycleSignal:
        if self.scope is ScopeKind.ENDPOINT and self.endpoint is None:
            raise ValueError("endpoint scope requires an endpoint")
        if self.scope is ScopeKind.SERVICE and self.endpoint is not None:
            raise ValueError("service scope cannot carry an endpoint")
        if self.endpoint is not None and self.endpoint.target_id != self.target_id:
            raise ValueError("endpoint target_id must match signal target_id")
        if not self.deprecated and self.deprecation_at is None and self.sunset_at is None:
            raise ValueError("a lifecycle signal must contain deprecation or sunset evidence")
        return self


class ScoreCard(FrozenModel):
    """Deterministic scoring result for one lifecycle record."""

    urgency: int = Field(ge=0, le=100)
    urgency_band: Band
    blast_radius: int = Field(ge=0, le=100)
    blast_radius_band: Band
    priority: int = Field(ge=0, le=100)
    priority_band: Band


class LifecycleRecord(FrozenModel):
    """Current reconciled view of one endpoint or service lifecycle."""

    id: str = Field(min_length=1, max_length=256)
    target_id: str
    scope: ScopeKind
    endpoints: tuple[EndpointRef, ...] = ()
    consumers: tuple[Consumer, ...] = ()
    first_seen_at: datetime
    last_seen_at: datetime
    effective_deprecation_at: datetime | None = None
    effective_sunset_at: datetime | None = None
    deprecated: bool = False
    active: bool = True
    date_conflict: bool = False
    state: LifecycleState
    score: ScoreCard
    scored_at: datetime

    @field_validator(
        "first_seen_at",
        "last_seen_at",
        "effective_deprecation_at",
        "effective_sunset_at",
        "scored_at",
    )
    @classmethod
    def normalize_record_datetimes(cls, value: datetime | None) -> datetime | None:
        return None if value is None else as_utc(value)

    @model_validator(mode="after")
    def validate_record(self) -> LifecycleRecord:
        if self.first_seen_at > self.last_seen_at:
            raise ValueError("first_seen_at must not be after last_seen_at")
        if self.scope is ScopeKind.ENDPOINT and not self.endpoints:
            raise ValueError("endpoint records require at least one endpoint")
        if self.scope is ScopeKind.SERVICE and self.endpoints:
            raise ValueError("service records must not embed endpoint scope")
        return self
