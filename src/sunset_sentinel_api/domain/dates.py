"""Strict HTTP-date parsing used by lifecycle headers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sunset_sentinel_api.domain.models import as_utc

_SHORT_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_LONG_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
_IMF_RE = re.compile(
    r"^(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun), "
    r"(?P<day>[0-9]{2}) (?P<month>[A-Z][a-z]{2}) (?P<year>[0-9]{4}) "
    r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2}) "
    r"(?P<zone>GMT|UTC)$"
)
_RFC850_RE = re.compile(
    r"^(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
    r"(?P<day>[0-9]{2})-(?P<month>[A-Z][a-z]{2})-(?P<year>[0-9]{2}) "
    r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2}) GMT$"
)
_ASCTIME_RE = re.compile(
    r"^(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"(?P<month>[A-Z][a-z]{2}) (?P<day> [0-9]|[0-9]{2}) "
    r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2}) "
    r"(?P<year>[0-9]{4})$"
)


@dataclass(frozen=True, slots=True)
class ParsedHttpDate:
    """A parsed HTTP date plus normalization metadata."""

    value: datetime
    obsolete: bool = False
    utc_alias: bool = False
    leap_second: bool = False


def _year_from_rfc850(two_digit_year: int, *, now: datetime) -> int:
    candidate = (now.year // 100) * 100 + two_digit_year
    if candidate > now.year + 50:
        candidate -= 100
    return candidate


def _build_datetime(match: re.Match[str], *, year: int) -> tuple[datetime, bool]:
    month_name = match.group("month")
    month = _MONTHS.get(month_name)
    if month is None:
        raise ValueError("HTTP-date contains an invalid month")

    second = int(match.group("second"))
    leap_second = second == 60
    if second > 60:
        raise ValueError("HTTP-date contains an invalid second")

    try:
        value = datetime(
            year,
            month,
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            59 if leap_second else second,
            tzinfo=UTC,
        )
    except ValueError as exc:
        raise ValueError("HTTP-date contains an invalid calendar date or time") from exc

    if leap_second:
        value += timedelta(seconds=1)
    return value, leap_second


def parse_http_date(
    value: str,
    *,
    now: datetime,
    allow_obsolete: bool = True,
    allow_utc_alias: bool = False,
) -> ParsedHttpDate:
    """Parse an RFC HTTP-date without locale-dependent or heuristic parsing."""

    normalized_now = as_utc(now, field_name="now")

    imf_match = _IMF_RE.fullmatch(value)
    if imf_match is not None:
        zone = imf_match.group("zone")
        if zone == "UTC" and not allow_utc_alias:
            raise ValueError("IMF-fixdate must use GMT")
        year = int(imf_match.group("year"))
        parsed, leap_second = _build_datetime(imf_match, year=year)
        input_weekday = imf_match.group("weekday")
        expected_weekday = _SHORT_WEEKDAYS[parsed.weekday()]
        if leap_second:
            base = parsed - timedelta(seconds=1)
            expected_weekday = _SHORT_WEEKDAYS[base.weekday()]
        if input_weekday != expected_weekday:
            raise ValueError("HTTP-date weekday does not match its calendar date")
        return ParsedHttpDate(
            value=parsed,
            utc_alias=zone == "UTC",
            leap_second=leap_second,
        )

    if not allow_obsolete:
        raise ValueError("value is not an IMF-fixdate")

    rfc850_match = _RFC850_RE.fullmatch(value)
    if rfc850_match is not None:
        year = _year_from_rfc850(int(rfc850_match.group("year")), now=normalized_now)
        parsed, leap_second = _build_datetime(rfc850_match, year=year)
        input_weekday = rfc850_match.group("weekday")
        comparison_date = parsed - timedelta(seconds=1) if leap_second else parsed
        if input_weekday != _LONG_WEEKDAYS[comparison_date.weekday()]:
            raise ValueError("HTTP-date weekday does not match its calendar date")
        return ParsedHttpDate(value=parsed, obsolete=True, leap_second=leap_second)

    asctime_match = _ASCTIME_RE.fullmatch(value)
    if asctime_match is not None:
        parsed, leap_second = _build_datetime(
            asctime_match,
            year=int(asctime_match.group("year")),
        )
        input_weekday = asctime_match.group("weekday")
        comparison_date = parsed - timedelta(seconds=1) if leap_second else parsed
        if input_weekday != _SHORT_WEEKDAYS[comparison_date.weekday()]:
            raise ValueError("HTTP-date weekday does not match its calendar date")
        return ParsedHttpDate(value=parsed, obsolete=True, leap_second=leap_second)

    raise ValueError("value is not a valid HTTP-date")
