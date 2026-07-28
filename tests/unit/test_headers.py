from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sunset_sentinel_api.domain import (
    HeaderMode,
    ParseStatus,
    parse_deprecation_header,
    parse_documentation_links,
    parse_lifecycle_headers,
    parse_sunset_header,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_sunset_accepts_imf_fixdate_and_normalizes_ows() -> None:
    parsed = parse_sunset_header(
        "\tWed, 11 Nov 2026 11:11:11 GMT ",
        now=NOW,
        mode=HeaderMode.STRICT,
    )

    assert parsed.status is ParseStatus.VALID
    assert parsed.value == datetime(2026, 11, 11, 11, 11, 11, tzinfo=UTC)
    assert parsed.diagnostics == ()


@pytest.mark.parametrize(
    ("value", "expected_year"),
    [
        ("Sunday, 06-Nov-94 08:49:37 GMT", 1994),
        ("Sun Nov  6 08:49:37 1994", 1994),
    ],
)
def test_sunset_accepts_obsolete_http_dates_with_warning(
    value: str,
    expected_year: int,
) -> None:
    parsed = parse_sunset_header(value, now=NOW)

    assert parsed.status is ParseStatus.VALID
    assert parsed.value is not None
    assert parsed.value.year == expected_year
    assert {diagnostic.code for diagnostic in parsed.diagnostics} == {"obsolete_http_date"}


def test_sunset_utc_alias_is_compat_only() -> None:
    value = "Wed, 11 Nov 2026 11:11:11 UTC"

    compat = parse_sunset_header(value, now=NOW, mode=HeaderMode.COMPAT)
    strict = parse_sunset_header(value, now=NOW, mode=HeaderMode.STRICT)

    assert compat.status is ParseStatus.LEGACY
    assert {item.code for item in compat.diagnostics} == {"utc_timezone_alias"}
    assert strict.status is ParseStatus.INVALID


@pytest.mark.parametrize(
    "value",
    [
        "Thu, 11 Nov 2026 11:11:11 GMT",
        "Wed, 31 Feb 2026 11:11:11 GMT",
        "Wed, 11 Nov 2026 11:11:11 +0000",
        "Wed, 11 Nov 2026 11:11:11 GMT\r\nX-Evil: value",
    ],
)
def test_sunset_rejects_invalid_or_unsafe_values(value: str) -> None:
    parsed = parse_sunset_header(value, now=NOW)

    assert parsed.status is ParseStatus.INVALID
    assert parsed.value is None


def test_sunset_does_not_split_the_date_comma_or_accept_duplicates() -> None:
    valid = parse_sunset_header("Wed, 11 Nov 2026 11:11:11 GMT", now=NOW)
    duplicate = parse_sunset_header(
        [
            "Wed, 11 Nov 2026 11:11:11 GMT",
            "Thu, 12 Nov 2026 11:11:11 GMT",
        ],
        now=NOW,
    )

    assert valid.status is ParseStatus.VALID
    assert duplicate.status is ParseStatus.INVALID
    assert duplicate.diagnostics[0].code == "duplicate_sunset_header"


def test_deprecation_accepts_rfc_9745_structured_date() -> None:
    parsed = parse_deprecation_header("@1688169599", now=NOW, mode=HeaderMode.STRICT)

    assert parsed.status is ParseStatus.VALID
    assert parsed.value == datetime(2023, 6, 30, 23, 59, 59, tzinfo=UTC)
    assert parsed.deprecated is True


def test_deprecation_accepts_legal_parameters_and_negative_epoch() -> None:
    parsed = parse_deprecation_header(
        '@-1;source="vendor";blob=:dmVuZG9y:;label=%"sunset%20soon"',
        now=NOW,
    )

    assert parsed.status is ParseStatus.VALID
    assert parsed.value == datetime(1969, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert [item.code for item in parsed.diagnostics] == ["ignored_deprecation_parameters"]


def test_deprecation_legacy_boolean_and_http_date_are_compat_only() -> None:
    legacy_boolean = parse_deprecation_header("TRUE", now=NOW, mode=HeaderMode.COMPAT)
    legacy_date = parse_deprecation_header(
        "Sun, 11 Nov 2018 23:59:59 GMT",
        now=NOW,
        mode=HeaderMode.COMPAT,
    )

    assert legacy_boolean.status is ParseStatus.LEGACY
    assert legacy_boolean.deprecated is True
    assert legacy_boolean.value is None
    assert legacy_date.status is ParseStatus.LEGACY
    assert legacy_date.value == datetime(2018, 11, 11, 23, 59, 59, tzinfo=UTC)
    assert (
        parse_deprecation_header("true", now=NOW, mode=HeaderMode.STRICT).status
        is ParseStatus.INVALID
    )


@pytest.mark.parametrize(
    "value",
    ["false", "?1", "?0", "@", "@+1", "@1.5", "@1, @2", "@999999999999999"],
)
def test_deprecation_rejects_non_date_and_out_of_range_values(value: str) -> None:
    parsed = parse_deprecation_header(value, now=NOW)

    assert parsed.status is ParseStatus.INVALID
    assert parsed.deprecated is False


@pytest.mark.parametrize(
    "value",
    [
        '@1;source="供应商"',
        "@1;value=1234567890123456",
        "@1;value=1234567890123.1",
        "@1;blob=:a:",
        "@1;blob=:abc===",
        "@1;\tkey=value",
        '@1;label=%"UPPER%2FHEX"',
    ],
)
def test_deprecation_rejects_malformed_structured_parameters(value: str) -> None:
    parsed = parse_deprecation_header(value, now=NOW, mode=HeaderMode.STRICT)

    assert parsed.status is ParseStatus.INVALID
    assert parsed.value is None


def test_deprecation_rejects_duplicate_field_instances() -> None:
    parsed = parse_deprecation_header(["@1", "@2"], now=NOW)

    assert parsed.status is ParseStatus.INVALID
    assert parsed.diagnostics[0].code == "duplicate_deprecation_header"


def test_link_parser_extracts_only_lifecycle_documentation_urls() -> None:
    parsed = parse_documentation_links(
        (
            '</deprecations/orders>; rel="deprecation"; title="Orders, v1", '
            "<https://docs.example.test/sunset>; rel=sunset"
        ),
        response_url="https://api.example.test/v1/orders",
    )

    assert parsed.urls == (
        "https://api.example.test/deprecations/orders",
        "https://docs.example.test/sunset",
    )
    assert parsed.diagnostics == ()


def test_link_parser_reports_invalid_credentials_without_leaking_raw_value() -> None:
    parsed = parse_documentation_links("<https://user:secret@example.test/docs>; rel=deprecation")

    assert parsed.urls == ()
    assert parsed.diagnostics[0].code == "invalid_link_header"
    assert "secret" not in parsed.diagnostics[0].message
    assert parsed.diagnostics[0].raw_sha256 is not None


def test_combined_parser_is_case_insensitive_and_preserves_duplicates() -> None:
    parsed = parse_lifecycle_headers(
        [
            ("sunset", "Wed, 11 Nov 2026 11:11:11 GMT"),
            ("Deprecation", "@1688169599"),
            ("LINK", "</docs>; rel=deprecation"),
        ],
        now=NOW,
        response_url="https://api.example.test/v1/orders",
    )

    assert parsed.has_signal is True
    assert parsed.sunset.status is ParseStatus.VALID
    assert parsed.deprecation.status is ParseStatus.VALID
    assert parsed.documentation_urls == ("https://api.example.test/docs",)
