from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sunset_sentinel_api.adapters.file_sources import (
    MAX_SOURCE_BYTES,
    FileSourceError,
    load_consumers_file,
    load_file_sources,
    load_manual_feed_file,
    load_openapi_file,
)
from sunset_sentinel_api.domain import ScopeKind, SignalSource

_OBSERVED_AT = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
_REPOSITORY_ROOT = Path(__file__).parents[2]


def test_example_files_load_as_one_deterministic_batch() -> None:
    examples = _REPOSITORY_ROOT / "examples"

    first = load_file_sources(
        observed_at=_OBSERVED_AT,
        openapi_files={"fixture-api": examples / "openapi.yaml"},
        manual_feed_files=(examples / "manual-feed.yaml",),
        consumer_files=(examples / "consumers.json",),
    )
    second = load_file_sources(
        observed_at=_OBSERVED_AT,
        openapi_files={"fixture-api": examples / "openapi.yaml"},
        manual_feed_files=(examples / "manual-feed.yaml",),
        consumer_files=(examples / "consumers.json",),
    )

    assert first == second
    assert len(first.signals) == 3
    assert [signal.signal_key for signal in first.signals] == [
        "openapi:fixture-api\nGET\n/v1/orders",
        "openapi:fixture-api\nGET\n/v1/search",
        "manual:partner-catalog:manual:partner-catalog-v1",
    ]
    assert {signal.source for signal in first.signals} == {
        SignalSource.MANUAL,
        SignalSource.OPENAPI,
    }
    assert len(first.consumers) == 2
    assert len(first.dependencies) == 3


@pytest.mark.parametrize("version", ["3.0.0", "3.1.2"])
def test_openapi_supports_30_and_31_deprecated_operations(tmp_path: Path, version: str) -> None:
    source = tmp_path / "spec.json"
    source.write_text(
        json.dumps(
            {
                "openapi": version,
                "info": {"title": "test", "version": "1"},
                "paths": {
                    "/v1/items": {
                        "get": {
                            "operationId": "listItems",
                            "deprecated": True,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    batch = load_openapi_file(
        source,
        target_id="inventory",
        observed_at=_OBSERVED_AT,
    )

    assert len(batch.signals) == 1
    signal = batch.signals[0]
    assert signal.deprecated is True
    assert signal.deprecation_at is None
    assert signal.endpoint is not None
    assert signal.endpoint.operation_id == "listItems"
    assert signal.source is SignalSource.OPENAPI


def test_openapi_extension_is_strict_and_normalizes_dates(tmp_path: Path) -> None:
    source = tmp_path / "spec.yaml"
    source.write_text(
        """
openapi: 3.1.0
info:
  title: test
  version: "1"
paths:
  /v1/items:
    post:
      x-sunset-sentinel:
        deprecation_at: "2026-06-30T23:59:59+08:00"
        sunset_at: "2026-09-30T15:59:59Z"
        documentation_url: "https://docs.example.test/migrate"
        replacement: "POST /v2/items"
""".lstrip(),
        encoding="utf-8",
    )

    signal = load_openapi_file(
        source,
        target_id="inventory",
        observed_at=_OBSERVED_AT,
    ).signals[0]

    assert signal.deprecation_at == datetime(2026, 6, 30, 15, 59, 59, tzinfo=UTC)
    assert signal.sunset_at == datetime(2026, 9, 30, 15, 59, 59, tzinfo=UTC)
    assert signal.documentation_url == "https://docs.example.test/migrate"
    assert signal.replacement == "POST /v2/items"

    source.write_text(
        source.read_text(encoding="utf-8").replace(
            '        replacement: "POST /v2/items"',
            '        replacement: "POST /v2/items"\n        unexpected: true',
        ),
        encoding="utf-8",
    )
    with pytest.raises(FileSourceError, match="Extra inputs are not permitted"):
        load_openapi_file(
            source,
            target_id="inventory",
            observed_at=_OBSERVED_AT,
        )


@pytest.mark.parametrize(
    "bad_dates",
    [
        (
            '"2030-01-01"',
            '"2031-01-01T00:00:00Z"',
            "RFC 3339",
        ),
        (
            '"2031-01-01T00:00:00Z"',
            '"2030-01-01T00:00:00Z"',
            "must not be before",
        ),
    ],
)
def test_manual_feed_rejects_non_rfc3339_and_reverse_dates(
    tmp_path: Path,
    bad_dates: tuple[str, str, str],
) -> None:
    deprecation_at, sunset_at, message = bad_dates
    source = tmp_path / "feed.json"
    source.write_text(
        f"""
{{
  "schema_version": 1,
  "signals": [
    {{
      "signal_key": "retirement",
      "target_id": "payments",
      "scope": "service",
      "deprecation_at": {deprecation_at},
      "sunset_at": {sunset_at}
    }}
  ]
}}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(FileSourceError, match=message):
        load_manual_feed_file(source, observed_at=_OBSERVED_AT)


def test_manual_feed_enforces_schema_version_scope_and_extra_fields(
    tmp_path: Path,
) -> None:
    source = tmp_path / "feed.yaml"
    source.write_text(
        """
schema_version: 2
signals:
  - signal_key: retirement
    target_id: payments
    scope: service
    method: GET
    deprecated: true
    extra: rejected
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(FileSourceError) as exc_info:
        load_manual_feed_file(source, observed_at=_OBSERVED_AT)

    message = str(exc_info.value)
    assert "schema_version" in message
    assert "Extra inputs are not permitted" in message


def test_manual_feed_builds_service_and_endpoint_domain_signals(
    tmp_path: Path,
) -> None:
    source = tmp_path / "feed.yaml"
    source.write_text(
        """
schema_version: 1
signals:
  - signal_key: service-retirement
    target_id: payments
    scope: service
    deprecated: true
  - signal_key: endpoint-retirement
    target_id: payments
    scope: endpoint
    method: delete
    path: /v1/orders/{id}
    sunset_at: "2030-01-01T00:00:00Z"
""".lstrip(),
        encoding="utf-8",
    )

    signals = load_manual_feed_file(source, observed_at=_OBSERVED_AT).signals

    assert [signal.signal_key for signal in signals] == [
        "manual:payments:service-retirement",
        "manual:payments:endpoint-retirement",
    ]
    assert signals[0].scope is ScopeKind.SERVICE
    assert signals[0].endpoint is None
    assert signals[1].scope is ScopeKind.ENDPOINT
    assert signals[1].endpoint is not None
    assert signals[1].endpoint.method == "DELETE"


def test_manual_signal_keys_are_globally_scoped_by_target(tmp_path: Path) -> None:
    source = tmp_path / "feed.yaml"
    source.write_text(
        """
schema_version: 1
signals:
  - signal_key: retirement
    target_id: catalog-a
    scope: service
    deprecated: true
  - signal_key: retirement
    target_id: catalog-b
    scope: service
    deprecated: true
""".lstrip(),
        encoding="utf-8",
    )

    signals = load_manual_feed_file(source, observed_at=_OBSERVED_AT).signals

    assert [signal.signal_key for signal in signals] == [
        "manual:catalog-a:retirement",
        "manual:catalog-b:retirement",
    ]
    assert signals[0].source_ref == "feed.yaml#/signals/0"
    assert signals[1].source_ref == "feed.yaml#/signals/1"


def test_consumers_reject_unknown_consumer_and_duplicate_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "consumers.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "consumers": [
                    {"id": "checkout", "name": "Checkout"},
                    {"id": "checkout", "name": "Checkout duplicate"},
                ],
                "dependencies": [
                    {
                        "consumer_id": "unknown",
                        "target_id": "payments",
                        "method": "GET",
                        "path": "/v1/orders",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(FileSourceError, match="duplicate consumer id"):
        load_consumers_file(source)

    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["consumers"] = payload["consumers"][:1]
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FileSourceError, match="unknown consumer"):
        load_consumers_file(source)


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (
            "duplicate.json",
            '{"schema_version":1,"consumers":[],"consumers":[],"dependencies":[]}',
        ),
        (
            "duplicate.yaml",
            "schema_version: 1\nsignals: []\nsignals: []\n",
        ),
    ],
)
def test_json_and_yaml_duplicate_keys_are_rejected(tmp_path: Path, name: str, text: str) -> None:
    source = tmp_path / name
    source.write_text(text, encoding="utf-8")

    loader = (
        load_consumers_file
        if source.suffix == ".json"
        else lambda candidate: load_manual_feed_file(candidate, observed_at=_OBSERVED_AT)
    )
    with pytest.raises(FileSourceError) as exc_info:
        loader(source)

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "duplicate mapping key" in str(exc_info.value.__cause__)


def test_yaml_uses_safe_loader(tmp_path: Path) -> None:
    source = tmp_path / "feed.yaml"
    marker = tmp_path / "must-not-exist"
    source.write_text(
        f"!!python/object/apply:pathlib.Path.write_text ['{marker}', 'unsafe']",
        encoding="utf-8",
    )

    with pytest.raises(FileSourceError):
        load_manual_feed_file(source, observed_at=_OBSERVED_AT)
    assert not marker.exists()


def test_source_files_have_a_five_mibibyte_limit(tmp_path: Path) -> None:
    source = tmp_path / "feed.json"
    source.write_bytes(b" " * (MAX_SOURCE_BYTES + 1))

    with pytest.raises(FileSourceError, match="exceeds"):
        load_manual_feed_file(source, observed_at=_OBSERVED_AT)


def test_naive_observation_time_is_rejected_even_for_empty_sources(
    tmp_path: Path,
) -> None:
    source = tmp_path / "feed.json"
    source.write_text('{"schema_version":1,"signals":[]}', encoding="utf-8")

    with pytest.raises(ValueError, match="timezone"):
        load_manual_feed_file(source, observed_at=datetime(2026, 1, 1))
