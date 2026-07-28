"""Pure parsers for Sunset, Deprecation, and lifecycle documentation links."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit

from sunset_sentinel_api.domain.dates import ParsedHttpDate, parse_http_date
from sunset_sentinel_api.domain.enums import (
    DiagnosticSeverity,
    HeaderMode,
    ParseStatus,
)
from sunset_sentinel_api.domain.models import (
    Diagnostic,
    ParsedHeaderValue,
    ParsedLifecycleHeaders,
    ParsedLinks,
    as_utc,
    validate_http_url,
)

_MAX_LIFECYCLE_HEADER_LENGTH = 1024
_MAX_LINK_HEADER_LENGTH = 8192
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SF_KEY = r"[a-z*][a-z0-9_.*-]*"
_SF_STRING = r'"(?:[\x20-\x21\x23-\x24\x26-\x5b\x5d-\x7e]|%|\\["\\])*"'
_SF_SYMBOL = r"[A-Za-z*][!#$%&'*+\-.^_`|~0-9A-Za-z:/]*"
_SF_NUMBER = r"(?:-?[0-9]{1,15}|-?[0-9]{1,12}\.[0-9]{1,3})"
_SF_BYTES = (
    r":(?:[A-Za-z0-9+/]{4})*"
    r"(?:[A-Za-z0-9+/]{2}(?:==)?|[A-Za-z0-9+/]{3}=?)?:"
)
_SF_DISPLAY_STRING = r'%"(?:[\x20-\x21\x23-\x24\x26-\x5b\x5d-\x7e]|\\|%[0-9a-f]{2})*"'
_SF_BARE_ITEM = (
    rf"(?:{_SF_STRING}|{_SF_DISPLAY_STRING}|{_SF_SYMBOL}|{_SF_NUMBER}|"
    rf"{_SF_BYTES}|\?[01]|@-?[0-9]{{1,15}})"
)
_SF_PARAMETER = rf"; *{_SF_KEY}(?:={_SF_BARE_ITEM})?"
_SF_DATE_RE = re.compile(rf"^@(?P<seconds>-?[0-9]{{1,15}})(?P<parameters>(?:{_SF_PARAMETER})*)$")
_LINK_TARGET_RE = re.compile(r"^<(?P<target>[^>]*)>(?P<parameters>.*)$")
_LINK_PARAMETER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _diagnostic(
    *,
    code: str,
    severity: DiagnosticSeverity,
    field: str,
    message: str,
    raw_value: str,
) -> Diagnostic:
    return Diagnostic(
        code=code,
        severity=severity,
        field=field,
        message=message,
        raw_sha256=_digest(raw_value),
    )


def _values(value: str | Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _safe_header_value(value: str, *, limit: int) -> bool:
    if len(value) > limit:
        return False
    return all(
        (ord(character) >= 32 or character == "\t") and ord(character) != 127 for character in value
    )


def _invalid_singleton(
    *,
    field: str,
    code: str,
    message: str,
    raw_value: str,
) -> ParsedHeaderValue:
    diagnostic = _diagnostic(
        code=code,
        severity=DiagnosticSeverity.ERROR,
        field=field,
        message=message,
        raw_value=raw_value,
    )
    return ParsedHeaderValue(status=ParseStatus.INVALID, diagnostics=(diagnostic,))


def _http_date_diagnostics(
    parsed: ParsedHttpDate,
    *,
    field: str,
    raw_value: str,
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    if parsed.obsolete:
        diagnostics.append(
            _diagnostic(
                code="obsolete_http_date",
                severity=DiagnosticSeverity.WARNING,
                field=field,
                message="An obsolete HTTP-date form was accepted for interoperability.",
                raw_value=raw_value,
            )
        )
    if parsed.utc_alias:
        diagnostics.append(
            _diagnostic(
                code="utc_timezone_alias",
                severity=DiagnosticSeverity.WARNING,
                field=field,
                message="UTC was accepted as a compatibility alias; IMF-fixdate uses GMT.",
                raw_value=raw_value,
            )
        )
    if parsed.leap_second:
        diagnostics.append(
            _diagnostic(
                code="leap_second_normalized",
                severity=DiagnosticSeverity.WARNING,
                field=field,
                message="A leap second was normalized to the following UTC second.",
                raw_value=raw_value,
            )
        )
    return tuple(diagnostics)


def parse_sunset_header(
    value: str | Sequence[str] | None,
    *,
    now: datetime,
    mode: HeaderMode = HeaderMode.COMPAT,
) -> ParsedHeaderValue:
    """Parse the singleton RFC 8594 Sunset response field."""

    as_utc(now, field_name="now")
    raw_values = _values(value)
    if not raw_values:
        return ParsedHeaderValue(status=ParseStatus.ABSENT)
    if len(raw_values) != 1:
        return _invalid_singleton(
            field="Sunset",
            code="duplicate_sunset_header",
            message="Sunset is a singleton field and must occur exactly once.",
            raw_value="\n".join(raw_values),
        )

    raw_value = raw_values[0]
    if not _safe_header_value(raw_value, limit=_MAX_LIFECYCLE_HEADER_LENGTH):
        return _invalid_singleton(
            field="Sunset",
            code="unsafe_sunset_header",
            message="Sunset contains control characters or exceeds the size limit.",
            raw_value=raw_value,
        )

    normalized = raw_value.strip(" \t")
    try:
        parsed = parse_http_date(
            normalized,
            now=now,
            allow_obsolete=True,
            allow_utc_alias=mode is HeaderMode.COMPAT,
        )
    except ValueError as exc:
        return _invalid_singleton(
            field="Sunset",
            code="invalid_sunset_date",
            message=str(exc),
            raw_value=raw_value,
        )

    diagnostics = _http_date_diagnostics(parsed, field="Sunset", raw_value=raw_value)
    status = ParseStatus.LEGACY if parsed.utc_alias else ParseStatus.VALID
    return ParsedHeaderValue(status=status, value=parsed.value, diagnostics=diagnostics)


def _parse_structured_date(value: str) -> tuple[datetime, bool]:
    match = _SF_DATE_RE.fullmatch(value)
    if match is None:
        raise ValueError("Deprecation must be an RFC 9651 Structured Field Date.")
    seconds = int(match.group("seconds"))
    try:
        parsed = _EPOCH + timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ValueError("Deprecation date is outside years 1 through 9999.") from exc
    return parsed, bool(match.group("parameters"))


def parse_deprecation_header(
    value: str | Sequence[str] | None,
    *,
    now: datetime,
    mode: HeaderMode = HeaderMode.COMPAT,
) -> ParsedHeaderValue:
    """Parse RFC 9745, optionally accepting the obsolete draft syntax."""

    as_utc(now, field_name="now")
    raw_values = _values(value)
    if not raw_values:
        return ParsedHeaderValue(status=ParseStatus.ABSENT)
    if len(raw_values) != 1:
        return _invalid_singleton(
            field="Deprecation",
            code="duplicate_deprecation_header",
            message="Deprecation is a singleton Structured Field Item.",
            raw_value="\n".join(raw_values),
        )

    raw_value = raw_values[0]
    if not _safe_header_value(raw_value, limit=_MAX_LIFECYCLE_HEADER_LENGTH):
        return _invalid_singleton(
            field="Deprecation",
            code="unsafe_deprecation_header",
            message="Deprecation contains control characters or exceeds the size limit.",
            raw_value=raw_value,
        )

    normalized = raw_value.strip(" \t")
    try:
        parsed, has_parameters = _parse_structured_date(normalized)
    except ValueError as structured_error:
        if mode is HeaderMode.COMPAT and normalized.casefold() == "true":
            diagnostic = _diagnostic(
                code="legacy_deprecation_boolean",
                severity=DiagnosticSeverity.WARNING,
                field="Deprecation",
                message="The obsolete draft boolean was accepted; RFC 9745 requires a date.",
                raw_value=raw_value,
            )
            return ParsedHeaderValue(
                status=ParseStatus.LEGACY,
                deprecated=True,
                diagnostics=(diagnostic,),
            )

        if mode is HeaderMode.COMPAT:
            try:
                legacy_date = parse_http_date(
                    normalized,
                    now=now,
                    allow_obsolete=False,
                    allow_utc_alias=True,
                )
            except ValueError:
                pass
            else:
                legacy_diagnostics = [
                    _diagnostic(
                        code="legacy_deprecation_http_date",
                        severity=DiagnosticSeverity.WARNING,
                        field="Deprecation",
                        message=(
                            "The obsolete draft HTTP-date was accepted; "
                            "RFC 9745 requires @unix-seconds."
                        ),
                        raw_value=raw_value,
                    )
                ]
                legacy_diagnostics.extend(
                    _http_date_diagnostics(
                        legacy_date,
                        field="Deprecation",
                        raw_value=raw_value,
                    )
                )
                return ParsedHeaderValue(
                    status=ParseStatus.LEGACY,
                    value=legacy_date.value,
                    deprecated=legacy_date.value <= as_utc(now, field_name="now"),
                    diagnostics=tuple(legacy_diagnostics),
                )

        return _invalid_singleton(
            field="Deprecation",
            code="invalid_deprecation_header",
            message=str(structured_error),
            raw_value=raw_value,
        )

    parameter_diagnostics: tuple[Diagnostic, ...] = ()
    if has_parameters:
        parameter_diagnostics = (
            _diagnostic(
                code="ignored_deprecation_parameters",
                severity=DiagnosticSeverity.INFO,
                field="Deprecation",
                message="Unrecognized Structured Field parameters were ignored.",
                raw_value=raw_value,
            ),
        )
    return ParsedHeaderValue(
        status=ParseStatus.VALID,
        value=parsed,
        deprecated=parsed <= as_utc(now, field_name="now"),
        diagnostics=parameter_diagnostics,
    )


def _split_quoted(value: str, *, separator: str, track_angles: bool = False) -> list[str]:
    parts: list[str] = []
    start = 0
    quoted = False
    escaped = False
    angle_depth = 0
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quoted and character == "\\":
            escaped = True
            continue
        if character == '"':
            quoted = not quoted
            continue
        if track_angles and not quoted:
            if character == "<":
                angle_depth += 1
            elif character == ">":
                angle_depth -= 1
                if angle_depth < 0:
                    raise ValueError("Link field contains an unmatched '>'.")
        if character == separator and not quoted and angle_depth == 0:
            parts.append(value[start:index])
            start = index + 1
    if quoted or escaped or angle_depth != 0:
        raise ValueError("Link field contains an unterminated quoted string or URI reference.")
    parts.append(value[start:])
    return parts


def _unquote_parameter(value: str) -> str:
    if not value.startswith('"'):
        return value
    if len(value) < 2 or not value.endswith('"'):
        raise ValueError("Link parameter contains an unterminated quoted string.")
    result: list[str] = []
    escaped = False
    for character in value[1:-1]:
        if escaped:
            if character not in {'"', "\\"}:
                raise ValueError("Link quoted-string contains an invalid escape.")
            result.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        else:
            result.append(character)
    if escaped:
        raise ValueError("Link quoted-string ends with an escape.")
    return "".join(result)


def _parse_one_link(
    value: str,
    *,
    response_url: str | None,
) -> str | None:
    match = _LINK_TARGET_RE.fullmatch(value.strip(" \t"))
    if match is None:
        raise ValueError("Link value must start with an angle-bracket URI reference.")

    relations: set[str] = set()
    parameters = match.group("parameters")
    if parameters:
        if not parameters.lstrip(" \t").startswith(";"):
            raise ValueError("Link target must be followed by semicolon parameters.")
        for part in _split_quoted(parameters, separator=";"):
            normalized_part = part.strip(" \t")
            if not normalized_part:
                continue
            name, separator, raw_parameter_value = normalized_part.partition("=")
            if not _LINK_PARAMETER_NAME_RE.fullmatch(name):
                raise ValueError("Link contains an invalid parameter name.")
            if name.casefold() != "rel":
                continue
            if not separator:
                raise ValueError("Link rel parameter requires a value.")
            relations.update(_unquote_parameter(raw_parameter_value).casefold().split())

    if not relations.intersection({"deprecation", "sunset"}):
        return None

    target = match.group("target")
    resolved = urljoin(response_url, target) if response_url is not None else target
    parsed = urlsplit(resolved)
    if not parsed.scheme and response_url is None:
        raise ValueError("Relative Link targets require the response URL.")
    return validate_http_url(resolved, field_name="Link target")


def parse_documentation_links(
    value: str | Sequence[str] | None,
    *,
    response_url: str | None = None,
) -> ParsedLinks:
    """Extract deprecation/sunset policy URLs without fetching them."""

    raw_values = _values(value)
    urls: list[str] = []
    diagnostics: list[Diagnostic] = []
    for raw_value in raw_values:
        if not _safe_header_value(raw_value, limit=_MAX_LINK_HEADER_LENGTH):
            diagnostics.append(
                _diagnostic(
                    code="unsafe_link_header",
                    severity=DiagnosticSeverity.ERROR,
                    field="Link",
                    message="Link contains control characters or exceeds the size limit.",
                    raw_value=raw_value,
                )
            )
            continue
        try:
            link_values = _split_quoted(raw_value, separator=",", track_angles=True)
            for link_value in link_values:
                parsed_url = _parse_one_link(link_value, response_url=response_url)
                if parsed_url is not None and parsed_url not in urls:
                    urls.append(parsed_url)
        except ValueError as exc:
            diagnostics.append(
                _diagnostic(
                    code="invalid_link_header",
                    severity=DiagnosticSeverity.WARNING,
                    field="Link",
                    message=str(exc),
                    raw_value=raw_value,
                )
            )
    return ParsedLinks(urls=tuple(urls), diagnostics=tuple(diagnostics))


HeaderInput = Mapping[str, str] | Iterable[tuple[str, str]]


def parse_lifecycle_headers(
    headers: HeaderInput,
    *,
    now: datetime,
    mode: HeaderMode = HeaderMode.COMPAT,
    response_url: str | None = None,
) -> ParsedLifecycleHeaders:
    """Parse case-insensitive response fields while preserving duplicates."""

    grouped: dict[str, list[str]] = {}
    items = headers.items() if isinstance(headers, Mapping) else headers
    for name, value in items:
        grouped.setdefault(name.casefold(), []).append(value)

    sunset = parse_sunset_header(grouped.get("sunset"), now=now, mode=mode)
    deprecation = parse_deprecation_header(grouped.get("deprecation"), now=now, mode=mode)
    links = parse_documentation_links(grouped.get("link"), response_url=response_url)
    diagnostics = sunset.diagnostics + deprecation.diagnostics + links.diagnostics
    return ParsedLifecycleHeaders(
        sunset=sunset,
        deprecation=deprecation,
        documentation_urls=links.urls,
        diagnostics=diagnostics,
    )
