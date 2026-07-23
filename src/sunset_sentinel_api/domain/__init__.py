"""Public API for Sunset Sentinel's dependency-free domain core."""

from sunset_sentinel_api.domain.enums import (
    Band,
    Criticality,
    DiagnosticSeverity,
    HeaderMode,
    LifecycleState,
    ParseStatus,
    ScopeKind,
    SignalCompliance,
    SignalSource,
)
from sunset_sentinel_api.domain.headers import (
    parse_deprecation_header,
    parse_documentation_links,
    parse_lifecycle_headers,
    parse_sunset_header,
)
from sunset_sentinel_api.domain.lifecycle import determine_lifecycle_state
from sunset_sentinel_api.domain.models import (
    Consumer,
    ConsumerDependency,
    Diagnostic,
    EndpointRef,
    LifecycleRecord,
    LifecycleSignal,
    ParsedHeaderValue,
    ParsedLifecycleHeaders,
    ParsedLinks,
    ScoreCard,
)
from sunset_sentinel_api.domain.scoring import (
    blast_radius_band,
    blast_radius_score,
    priority_score,
    score_lifecycle,
    urgency_band,
    urgency_score,
)

__all__ = [
    "Band",
    "Consumer",
    "ConsumerDependency",
    "Criticality",
    "Diagnostic",
    "DiagnosticSeverity",
    "EndpointRef",
    "HeaderMode",
    "LifecycleRecord",
    "LifecycleSignal",
    "LifecycleState",
    "ParseStatus",
    "ParsedHeaderValue",
    "ParsedLifecycleHeaders",
    "ParsedLinks",
    "ScopeKind",
    "ScoreCard",
    "SignalCompliance",
    "SignalSource",
    "blast_radius_band",
    "blast_radius_score",
    "determine_lifecycle_state",
    "parse_deprecation_header",
    "parse_documentation_links",
    "parse_lifecycle_headers",
    "parse_sunset_header",
    "priority_score",
    "score_lifecycle",
    "urgency_band",
    "urgency_score",
]
