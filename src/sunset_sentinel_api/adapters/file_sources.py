"""Strict, deterministic adapters for local lifecycle source files."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Self, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from sunset_sentinel_api.domain import (
    Consumer,
    ConsumerDependency,
    Criticality,
    EndpointRef,
    LifecycleSignal,
    ScopeKind,
    SignalSource,
)
from sunset_sentinel_api.domain.models import as_utc

MAX_SOURCE_BYTES = 5 * 1024 * 1024

_OPENAPI_VERSION_RE = re.compile(r"^3\.(?:0|1)\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")
_HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})
_YAML_MAPPING_TAG = "tag:yaml.org,2002:map"


class FileSourceError(ValueError):
    """A local source file could not be decoded or validated safely."""


class _DuplicateKeyError(ValueError):
    """A serialized mapping contains an ambiguous duplicate key."""


@dataclass(frozen=True, slots=True)
class SourceBatch:
    """Normalized source data ready for reconciliation and persistence."""

    signals: tuple[LifecycleSignal, ...] = ()
    consumers: tuple[Consumer, ...] = ()
    dependencies: tuple[ConsumerDependency, ...] = ()
    authoritative_openapi_targets: tuple[str, ...] = ()
    manual_signals_authoritative: bool = False
    consumers_authoritative: bool = False


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _parse_rfc3339(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be an RFC 3339 timestamp with seconds and a timezone")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid calendar timestamp") from exc
    return as_utc(parsed, field_name=field_name)


class _DatedInput(_StrictInput):
    deprecation_at: datetime | None = None
    sunset_at: datetime | None = None

    @field_validator("deprecation_at", "sunset_at", mode="before")
    @classmethod
    def parse_dates(cls, value: object, info: ValidationInfo) -> datetime | None:
        if value is None:
            return None
        return _parse_rfc3339(value, field_name=info.field_name or "datetime")

    @model_validator(mode="after")
    def validate_date_order(self) -> Self:
        if (
            self.deprecation_at is not None
            and self.sunset_at is not None
            and self.sunset_at < self.deprecation_at
        ):
            raise ValueError("sunset_at must not be before deprecation_at")
        return self


class _OpenAPIExtension(_DatedInput):
    deprecated: bool = False
    documentation_url: str | None = Field(default=None, max_length=2048)
    replacement: str | None = Field(default=None, max_length=2048)
    active: bool = True


class _ManualSignalInput(_DatedInput):
    signal_key: str = Field(min_length=1, max_length=512)
    target_id: str
    scope: Literal["endpoint", "service"]
    method: str | None = None
    path: str | None = Field(default=None, min_length=1, max_length=2048)
    operation_id: str | None = Field(default=None, max_length=256)
    deprecated: bool = False
    documentation_url: str | None = Field(default=None, max_length=2048)
    replacement: str | None = Field(default=None, max_length=2048)
    active: bool = True

    @model_validator(mode="after")
    def validate_scope_and_evidence(self) -> Self:
        endpoint_fields = (self.method, self.path, self.operation_id)
        if self.scope == "endpoint" and (self.method is None or self.path is None):
            raise ValueError("endpoint scope requires method and path")
        if self.scope == "service" and any(value is not None for value in endpoint_fields):
            raise ValueError("service scope forbids method, path, and operation_id")
        if not self.deprecated and self.deprecation_at is None and self.sunset_at is None:
            raise ValueError("a manual signal requires deprecation or sunset evidence")
        return self


class _ManualFeedInput(_StrictInput):
    schema_version: Literal[1]
    license: Literal["MIT"] | None = None
    signals: list[_ManualSignalInput]


class _ConsumerInput(_StrictInput):
    id: str
    name: str = Field(min_length=1, max_length=256)
    criticality: Literal["low", "medium", "high", "critical"] = "medium"
    owner: str | None = Field(default=None, max_length=256)
    repository_path: str | None = Field(default=None, max_length=2048)
    tags: list[str] = Field(default_factory=list)


class _DependencyInput(_StrictInput):
    consumer_id: str
    target_id: str
    method: str
    path: str = Field(min_length=1, max_length=2048)
    evidence: str | None = Field(default=None, max_length=2048)


class _ConsumerFileInput(_StrictInput):
    schema_version: Literal[1]
    license: Literal["MIT"] | None = None
    consumers: list[_ConsumerInput]
    dependencies: list[_DependencyInput]


class _UniqueKeySafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """PyYAML safe loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise _DuplicateKeyError("YAML mapping keys must be scalar values") from exc
        if duplicate:
            raise _DuplicateKeyError(f"duplicate mapping key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(_YAML_MAPPING_TAG, _construct_unique_mapping)


def _reject_json_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate mapping key {key!r}")
        result[key] = value
    return result


def _load_yaml(text: str) -> object:
    loader = _UniqueKeySafeLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _read_document(path: str | Path) -> tuple[Path, bytes, object]:
    source_path = Path(path)
    try:
        with source_path.open("rb") as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        detail = exc.strerror or exc.__class__.__name__
        raise FileSourceError(f"cannot read {source_path}: {detail}") from exc
    if len(raw) > MAX_SOURCE_BYTES:
        raise FileSourceError(f"{source_path} exceeds the {MAX_SOURCE_BYTES}-byte source limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileSourceError(f"{source_path} must be UTF-8 encoded") from exc

    suffix = source_path.suffix.lower()
    try:
        if suffix == ".json":
            document = json.loads(text, object_pairs_hook=_reject_json_duplicates)
        elif suffix in {".yaml", ".yml"}:
            document = _load_yaml(text)
        else:
            raise FileSourceError(f"{source_path} must use a .json, .yaml, or .yml extension")
    except (json.JSONDecodeError, yaml.YAMLError, _DuplicateKeyError) as exc:
        kind = suffix[1:].upper()
        raise FileSourceError(f"{source_path} is not valid, unambiguous {kind}") from exc
    return source_path, raw, document


def _as_string_mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FileSourceError(f"{context} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise FileSourceError(f"{context} must use string mapping keys")
    return cast(Mapping[str, object], value)


def _format_validation_error(context: str, error: ValidationError) -> FileSourceError:
    details: list[str] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        details.append(f"{location}: {item['msg']}")
    return FileSourceError(f"{context} failed schema validation: {'; '.join(details)}")


def _source_ref(path: Path, pointer: str) -> str:
    return f"{path.name}#{pointer}"


def _signal_key(prefix: str, identity: str) -> str:
    candidate = f"{prefix}:{identity}"
    if len(candidate) <= 512:
        return candidate
    return f"{prefix}:sha256:{sha256(identity.encode('utf-8')).hexdigest()}"


def _sort_signals(
    signals: Sequence[LifecycleSignal],
) -> tuple[LifecycleSignal, ...]:
    return tuple(
        sorted(
            signals,
            key=lambda signal: (
                signal.target_id,
                "" if signal.endpoint is None else signal.endpoint.key,
                signal.signal_key,
                signal.source_ref,
            ),
        )
    )


def load_openapi_file(
    path: str | Path,
    *,
    target_id: str,
    observed_at: datetime,
) -> SourceBatch:
    """Load OpenAPI 3.0/3.1 operation lifecycle evidence from JSON or YAML."""

    normalized_observed_at = as_utc(observed_at, field_name="observed_at")
    source_path, raw, document = _read_document(path)
    root = _as_string_mapping(document, context=str(source_path))

    version = root.get("openapi")
    if not isinstance(version, str) or not _OPENAPI_VERSION_RE.fullmatch(version):
        raise FileSourceError(f"{source_path} must declare OpenAPI 3.0.x or 3.1.x")
    paths = _as_string_mapping(root.get("paths"), context=f"{source_path}: paths")
    raw_sha256 = sha256(raw).hexdigest()
    signals: list[LifecycleSignal] = []

    for endpoint_path in sorted(paths):
        if not endpoint_path.startswith("/"):
            raise FileSourceError(f"{source_path}: OpenAPI path keys must start with '/'")
        path_item = _as_string_mapping(
            paths[endpoint_path], context=f"{source_path}: paths.{endpoint_path}"
        )
        for method in sorted(_HTTP_METHODS.intersection(path_item)):
            operation = _as_string_mapping(
                path_item[method],
                context=f"{source_path}: paths.{endpoint_path}.{method}",
            )
            deprecated_value = operation.get("deprecated", False)
            if type(deprecated_value) is not bool:
                raise FileSourceError(
                    f"{source_path}: paths.{endpoint_path}.{method}.deprecated must be a boolean"
                )
            operation_id = operation.get("operationId")
            if operation_id is not None and not isinstance(operation_id, str):
                raise FileSourceError(
                    f"{source_path}: paths.{endpoint_path}.{method}.operationId must be a string"
                )

            extension: _OpenAPIExtension | None = None
            if "x-sunset-sentinel" in operation:
                extension_value = _as_string_mapping(
                    operation["x-sunset-sentinel"],
                    context=(f"{source_path}: paths.{endpoint_path}.{method}.x-sunset-sentinel"),
                )
                try:
                    extension = _OpenAPIExtension.model_validate(extension_value)
                except ValidationError as exc:
                    raise _format_validation_error(
                        (f"{source_path}: paths.{endpoint_path}.{method}.x-sunset-sentinel"),
                        exc,
                    ) from exc

            deprecated = deprecated_value or (extension is not None and extension.deprecated)
            deprecation_at = None if extension is None else extension.deprecation_at
            sunset_at = None if extension is None else extension.sunset_at
            if not deprecated and deprecation_at is None and sunset_at is None:
                if extension is not None:
                    raise FileSourceError(
                        f"{source_path}: paths.{endpoint_path}.{method} extension "
                        "contains no lifecycle evidence"
                    )
                continue

            endpoint = EndpointRef(
                target_id=target_id,
                method=method,
                path=endpoint_path,
                operation_id=operation_id,
            )
            pointer_path = endpoint_path.replace("~", "~0").replace("/", "~1")
            signals.append(
                LifecycleSignal(
                    signal_key=_signal_key("openapi", endpoint.key),
                    target_id=target_id,
                    source=SignalSource.OPENAPI,
                    source_ref=_source_ref(source_path, f"/paths/{pointer_path}/{method}"),
                    scope=ScopeKind.ENDPOINT,
                    endpoint=endpoint,
                    deprecated=deprecated,
                    deprecation_at=deprecation_at,
                    sunset_at=sunset_at,
                    documentation_url=(None if extension is None else extension.documentation_url),
                    replacement=None if extension is None else extension.replacement,
                    observed_at=normalized_observed_at,
                    active=True if extension is None else extension.active,
                    raw_sha256=raw_sha256,
                )
            )

    return SourceBatch(
        signals=_sort_signals(signals),
        authoritative_openapi_targets=(target_id,),
    )


def load_manual_feed_file(
    path: str | Path,
    *,
    observed_at: datetime,
) -> SourceBatch:
    """Load a strict schema_version=1 manual lifecycle feed."""

    normalized_observed_at = as_utc(observed_at, field_name="observed_at")
    source_path, raw, document = _read_document(path)
    root = _as_string_mapping(document, context=str(source_path))
    try:
        feed = _ManualFeedInput.model_validate(root)
    except ValidationError as exc:
        raise _format_validation_error(str(source_path), exc) from exc

    raw_sha256 = sha256(raw).hexdigest()
    identities: set[tuple[str, str]] = set()
    signals: list[LifecycleSignal] = []
    for index, item in enumerate(feed.signals):
        identity = (item.target_id, item.signal_key)
        if identity in identities:
            raise FileSourceError(f"{source_path}: duplicate manual signal identity {identity!r}")
        identities.add(identity)

        endpoint: EndpointRef | None = None
        if item.scope == "endpoint":
            endpoint = EndpointRef(
                target_id=item.target_id,
                method=cast(str, item.method),
                path=cast(str, item.path),
                operation_id=item.operation_id,
            )
        signals.append(
            LifecycleSignal(
                signal_key=_signal_key(
                    "manual",
                    f"{item.target_id}:{item.signal_key}",
                ),
                target_id=item.target_id,
                source=SignalSource.MANUAL,
                source_ref=_source_ref(source_path, f"/signals/{index}"),
                scope=ScopeKind(item.scope),
                endpoint=endpoint,
                deprecated=item.deprecated,
                deprecation_at=item.deprecation_at,
                sunset_at=item.sunset_at,
                documentation_url=item.documentation_url,
                replacement=item.replacement,
                observed_at=normalized_observed_at,
                active=item.active,
                raw_sha256=raw_sha256,
            )
        )
    return SourceBatch(
        signals=_sort_signals(signals),
        manual_signals_authoritative=True,
    )


def load_consumers_file(path: str | Path) -> SourceBatch:
    """Load strict schema_version=1 consumers and dependency edges."""

    source_path, _, document = _read_document(path)
    root = _as_string_mapping(document, context=str(source_path))
    try:
        source = _ConsumerFileInput.model_validate(root)
    except ValidationError as exc:
        raise _format_validation_error(str(source_path), exc) from exc

    consumer_ids: set[str] = set()
    consumers: list[Consumer] = []
    for consumer_input in source.consumers:
        if consumer_input.id in consumer_ids:
            raise FileSourceError(f"{source_path}: duplicate consumer id {consumer_input.id!r}")
        consumer_ids.add(consumer_input.id)
        consumers.append(
            Consumer(
                id=consumer_input.id,
                name=consumer_input.name,
                criticality=Criticality(consumer_input.criticality),
                owner=consumer_input.owner,
                repository_path=consumer_input.repository_path,
                tags=tuple(consumer_input.tags),
            )
        )

    dependency_ids: set[tuple[str, str]] = set()
    dependencies: list[ConsumerDependency] = []
    for dependency_input in source.dependencies:
        if dependency_input.consumer_id not in consumer_ids:
            raise FileSourceError(
                f"{source_path}: dependency references unknown consumer "
                f"{dependency_input.consumer_id!r}"
            )
        endpoint = EndpointRef(
            target_id=dependency_input.target_id,
            method=dependency_input.method,
            path=dependency_input.path,
        )
        dependency_identity = (dependency_input.consumer_id, endpoint.key)
        if dependency_identity in dependency_ids:
            raise FileSourceError(
                f"{source_path}: duplicate consumer dependency {dependency_identity!r}"
            )
        dependency_ids.add(dependency_identity)
        dependencies.append(
            ConsumerDependency(
                consumer_id=dependency_input.consumer_id,
                endpoint_key=endpoint.key,
                evidence=dependency_input.evidence,
            )
        )

    return SourceBatch(
        consumers=tuple(sorted(consumers, key=lambda consumer: consumer.id)),
        dependencies=tuple(
            sorted(
                dependencies,
                key=lambda dependency: (
                    dependency.consumer_id,
                    dependency.endpoint_key,
                ),
            )
        ),
        consumers_authoritative=True,
    )


def merge_source_batches(*batches: SourceBatch) -> SourceBatch:
    """Merge source batches with deterministic ordering and strict identity checks."""

    signals: list[LifecycleSignal] = []
    consumers: list[Consumer] = []
    dependencies: list[ConsumerDependency] = []
    authoritative_openapi_targets: set[str] = set()
    manual_signals_authoritative = False
    consumers_authoritative = False
    signal_ids: set[tuple[str, SignalSource, str]] = set()
    consumer_ids: set[str] = set()
    dependency_ids: set[tuple[str, str]] = set()

    for batch in batches:
        authoritative_openapi_targets.update(batch.authoritative_openapi_targets)
        manual_signals_authoritative |= batch.manual_signals_authoritative
        consumers_authoritative |= batch.consumers_authoritative
        for signal in batch.signals:
            signal_identity = (signal.target_id, signal.source, signal.signal_key)
            if signal_identity in signal_ids:
                raise FileSourceError(f"duplicate signal identity {signal_identity!r}")
            signal_ids.add(signal_identity)
            signals.append(signal)
        for consumer in batch.consumers:
            if consumer.id in consumer_ids:
                raise FileSourceError(f"duplicate consumer id {consumer.id!r}")
            consumer_ids.add(consumer.id)
            consumers.append(consumer)
        for dependency in batch.dependencies:
            dependency_identity = (
                dependency.consumer_id,
                dependency.endpoint_key,
            )
            if dependency_identity in dependency_ids:
                raise FileSourceError(f"duplicate consumer dependency {dependency_identity!r}")
            dependency_ids.add(dependency_identity)
            dependencies.append(dependency)

    unknown_consumers = sorted(
        {
            dependency.consumer_id
            for dependency in dependencies
            if dependency.consumer_id not in consumer_ids
        }
    )
    if unknown_consumers:
        raise FileSourceError(
            f"dependencies reference unknown consumers: {', '.join(unknown_consumers)}"
        )

    return SourceBatch(
        signals=_sort_signals(signals),
        consumers=tuple(sorted(consumers, key=lambda consumer: consumer.id)),
        dependencies=tuple(
            sorted(
                dependencies,
                key=lambda dependency: (
                    dependency.consumer_id,
                    dependency.endpoint_key,
                ),
            )
        ),
        authoritative_openapi_targets=tuple(sorted(authoritative_openapi_targets)),
        manual_signals_authoritative=manual_signals_authoritative,
        consumers_authoritative=consumers_authoritative,
    )


def load_file_sources(
    *,
    observed_at: datetime,
    openapi_files: Mapping[str, str | Path] | None = None,
    manual_feed_files: Sequence[str | Path] = (),
    consumer_files: Sequence[str | Path] = (),
) -> SourceBatch:
    """Load and merge a deterministic set of local files into one source batch."""

    batches: list[SourceBatch] = []
    for target_id, path in sorted((openapi_files or {}).items()):
        batches.append(
            load_openapi_file(
                path,
                target_id=target_id,
                observed_at=observed_at,
            )
        )
    batches.extend(
        load_manual_feed_file(path, observed_at=observed_at)
        for path in sorted(manual_feed_files, key=lambda item: str(item))
    )
    batches.extend(
        load_consumers_file(path) for path in sorted(consumer_files, key=lambda item: str(item))
    )
    return merge_source_batches(*batches)


__all__ = [
    "MAX_SOURCE_BYTES",
    "FileSourceError",
    "SourceBatch",
    "load_consumers_file",
    "load_file_sources",
    "load_manual_feed_file",
    "load_openapi_file",
    "merge_source_batches",
]
