"""Enumerations shared by the domain core."""

from __future__ import annotations

from enum import StrEnum


class SignalSource(StrEnum):
    """Where a lifecycle signal originated."""

    HTTP_HEADER = "http_header"
    OPENAPI = "openapi"
    MANUAL = "manual"


class ScopeKind(StrEnum):
    """The resource scope covered by a signal."""

    ENDPOINT = "endpoint"
    SERVICE = "service"


class ParseStatus(StrEnum):
    """Outcome of parsing one external value."""

    ABSENT = "absent"
    VALID = "valid"
    LEGACY = "legacy"
    INVALID = "invalid"


class HeaderMode(StrEnum):
    """Standards policy for parsing HTTP lifecycle fields."""

    STRICT = "strict"
    COMPAT = "compat"


class SignalCompliance(StrEnum):
    """Standards status of accepted evidence."""

    RFC = "rfc"
    LEGACY = "legacy"


class DiagnosticSeverity(StrEnum):
    """Severity of a non-secret parsing or lifecycle diagnostic."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LifecycleState(StrEnum):
    """Current state derived from lifecycle evidence."""

    ACTIVE = "active"
    DEPRECATION_SCHEDULED = "deprecation_scheduled"
    DEPRECATED = "deprecated"
    DEPRECATED_DATE_UNKNOWN = "deprecated_date_unknown"
    SUNSET_SCHEDULED = "sunset_scheduled"
    SUNSET_OVERDUE = "sunset_overdue"
    CONFLICTED = "conflicted"
    WITHDRAWN = "withdrawn"


class Criticality(StrEnum):
    """Business criticality of a local API consumer."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Band(StrEnum):
    """Human-readable score band."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
