"""A narrowly scoped, cache-aware HTTP client for lifecycle response fields."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Lock
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

from sunset_sentinel_api.domain.dates import parse_http_date

_RETAINED_RESPONSE_HEADERS = frozenset(
    {
        "sunset",
        "deprecation",
        "link",
        "etag",
        "last-modified",
        "cache-control",
        "date",
        "retry-after",
    }
)
_CONDITIONAL_HEADERS = frozenset({"if-none-match", "if-modified-since"})
_FORBIDDEN_REQUEST_OVERRIDES = frozenset(
    {"host", "connection", "proxy-authorization", "proxy-authenticate"}
)
_LINK_TARGET_RE = re.compile(r"<([^<>]*)>")


class Clock(Protocol):
    """Wall clock used for cache and rate-limit decisions."""

    def now(self) -> datetime:
        """Return the current timezone-aware instant."""


class FetchStatus(StrEnum):
    """A complete, non-exceptional outcome of a lifecycle request."""

    SUCCESS = "success"
    CACHE_HIT = "cache_hit"
    NOT_MODIFIED = "not_modified"
    RATE_LIMITED = "rate_limited"
    RETRY_LATER = "retry_later"
    REDIRECT_BLOCKED = "redirect_blocked"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    BLOCKED = "blocked"


HeaderTuple = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class CachedLifecycleResponse:
    """Cache entry containing lifecycle metadata and never a response body."""

    key: str
    url: str
    status_code: int
    headers: HeaderTuple
    fetched_at: datetime
    validated_at: datetime
    expires_at: datetime


class CacheStore(Protocol):
    """Storage port that can later be backed by SQLite."""

    def get(self, key: str) -> CachedLifecycleResponse | None:
        """Return an entry without changing its freshness."""

    def put(self, entry: CachedLifecycleResponse) -> None:
        """Insert or replace one entry."""


class InMemoryCache:
    """Thread-safe in-process cache useful for the CLI and deterministic tests."""

    def __init__(self) -> None:
        self._entries: dict[str, CachedLifecycleResponse] = {}
        self._lock = Lock()

    def get(self, key: str) -> CachedLifecycleResponse | None:
        with self._lock:
            return self._entries.get(key)

    def put(self, entry: CachedLifecycleResponse) -> None:
        with self._lock:
            self._entries[entry.key] = entry


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Safe result returned for every attempted lifecycle fetch."""

    status: FetchStatus
    url: str
    status_code: int | None = None
    network_status_code: int | None = None
    headers: HeaderTuple = ()
    fetched_at: datetime | None = None
    validated_at: datetime | None = None
    expires_at: datetime | None = None
    next_request_at: datetime | None = None
    stale: bool = False
    error_code: str | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the result contains successfully obtained lifecycle metadata."""

        return self.status in {
            FetchStatus.SUCCESS,
            FetchStatus.CACHE_HIT,
            FetchStatus.NOT_MODIFIED,
        }

    def header_values(self, name: str) -> tuple[str, ...]:
        """Return all retained values for a case-insensitive field name."""

        normalized = name.casefold()
        return tuple(value for field, value in self.headers if field == normalized)


@dataclass(frozen=True, slots=True)
class _ValidatedUrl:
    request_url: str
    safe_url: str
    cache_key: str
    origin: str


class _PolicyViolation(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HttpLifecycleClient:
    """Fetch only lifecycle headers from explicitly allowed HTTP origins."""

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str],
        clock: Clock,
        cache: CacheStore | None = None,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        allow_loopback: bool = False,
        default_ttl_seconds: int = 3600,
        maximum_ttl_seconds: int = 21600,
        minimum_origin_interval_seconds: int = 60,
        retry_after_fallback_seconds: int = 60,
        maximum_retry_after_seconds: int = 86400,
        timeout_seconds: float = 10.0,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("client and transport are mutually exclusive")
        for name, value in {
            "default_ttl_seconds": default_ttl_seconds,
            "maximum_ttl_seconds": maximum_ttl_seconds,
            "minimum_origin_interval_seconds": minimum_origin_interval_seconds,
            "retry_after_fallback_seconds": retry_after_fallback_seconds,
            "maximum_retry_after_seconds": maximum_retry_after_seconds,
        }.items():
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        if default_ttl_seconds > maximum_ttl_seconds:
            raise ValueError("default_ttl_seconds must not exceed maximum_ttl_seconds")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        exact_hosts: set[str] = set()
        wildcard_suffixes: set[str] = set()
        for entry in allowed_hosts:
            normalized, wildcard = _normalize_allowlist_entry(entry)
            if wildcard:
                wildcard_suffixes.add(normalized)
            else:
                exact_hosts.add(normalized)
        if not exact_hosts and not wildcard_suffixes:
            raise ValueError("allowed_hosts must not be empty")

        self._exact_hosts = frozenset(exact_hosts)
        self._wildcard_suffixes = frozenset(wildcard_suffixes)
        self._clock = clock
        self._cache = cache if cache is not None else InMemoryCache()
        self._allow_loopback = allow_loopback
        self._default_ttl = timedelta(seconds=default_ttl_seconds)
        self._maximum_ttl = timedelta(seconds=maximum_ttl_seconds)
        self._minimum_origin_interval = timedelta(seconds=minimum_origin_interval_seconds)
        self._retry_after_fallback = timedelta(seconds=retry_after_fallback_seconds)
        self._maximum_retry_after = timedelta(seconds=maximum_retry_after_seconds)
        self._last_request_at: dict[str, datetime] = {}
        self._blocked_until: dict[str, datetime] = {}
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.Client(
                transport=transport,
                timeout=timeout_seconds,
                follow_redirects=False,
            )
        )

    def close(self) -> None:
        """Close an internally constructed httpx client."""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HttpLifecycleClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch(
        self,
        url: str,
        *,
        request_headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        """Fetch lifecycle headers, using cache validators when stale."""

        safe_url = redact_query_values(url)
        try:
            validated = self._validate_url(url)
        except _PolicyViolation as exc:
            return FetchResult(
                status=FetchStatus.BLOCKED,
                url=safe_url,
                error_code=exc.code,
                message=str(exc),
            )

        now = _utc_now(self._clock)
        cached = self._cache.get(validated.cache_key)
        if cached is not None and cached.expires_at > now:
            return self._result_from_cache(
                status=FetchStatus.CACHE_HIT,
                validated=validated,
                cached=cached,
                now=now,
            )

        next_request_at = self._next_request_at(validated.origin)
        if next_request_at is not None and now < next_request_at:
            if cached is not None:
                return self._result_from_cache(
                    status=FetchStatus.RATE_LIMITED,
                    validated=validated,
                    cached=cached,
                    now=now,
                    next_request_at=next_request_at,
                )
            return FetchResult(
                status=FetchStatus.RATE_LIMITED,
                url=validated.safe_url,
                next_request_at=next_request_at,
                error_code="origin_rate_limited",
                message="The origin is not eligible for another request yet.",
            )

        transient_headers = self._request_headers(request_headers, cached=cached)
        request = self._client.build_request(
            "GET",
            validated.request_url,
            headers=transient_headers,
        )
        self._last_request_at[validated.origin] = now

        try:
            response = self._client.send(request, stream=True, follow_redirects=False)
        except httpx.TimeoutException:
            return FetchResult(
                status=FetchStatus.TIMEOUT,
                url=validated.safe_url,
                error_code="request_timeout",
                message="The lifecycle request timed out.",
            )
        except httpx.HTTPError:
            return FetchResult(
                status=FetchStatus.NETWORK_ERROR,
                url=validated.safe_url,
                error_code="network_error",
                message="The lifecycle request failed before receiving a response.",
            )

        try:
            return self._handle_response(
                response=response,
                validated=validated,
                cached=cached,
                now=now,
            )
        finally:
            response.close()

    def _handle_response(
        self,
        *,
        response: httpx.Response,
        validated: _ValidatedUrl,
        cached: CachedLifecycleResponse | None,
        now: datetime,
    ) -> FetchResult:
        retained = _retained_headers(response.headers)
        status_code = response.status_code

        if 300 <= status_code < 400 and status_code != 304:
            return FetchResult(
                status=FetchStatus.REDIRECT_BLOCKED,
                url=validated.safe_url,
                network_status_code=status_code,
                headers=retained,
                validated_at=now,
                error_code="redirect_not_followed",
                message="Redirects are disabled for lifecycle requests.",
            )

        if status_code == 304:
            if cached is None:
                return FetchResult(
                    status=FetchStatus.HTTP_ERROR,
                    url=validated.safe_url,
                    network_status_code=304,
                    headers=retained,
                    validated_at=now,
                    error_code="unexpected_not_modified",
                    message="A 304 response was received without a cache entry.",
                )
            merged_headers = _merge_headers(cached.headers, retained)
            expires_at, cacheable = self._expiration(now=now, headers=merged_headers)
            updated = CachedLifecycleResponse(
                key=validated.cache_key,
                url=validated.safe_url,
                status_code=cached.status_code,
                headers=merged_headers,
                fetched_at=cached.fetched_at,
                validated_at=now,
                expires_at=expires_at,
            )
            if cacheable:
                self._cache.put(updated)
            return FetchResult(
                status=FetchStatus.NOT_MODIFIED,
                url=validated.safe_url,
                status_code=cached.status_code,
                network_status_code=304,
                headers=merged_headers,
                fetched_at=cached.fetched_at,
                validated_at=now,
                expires_at=expires_at,
            )

        if status_code in {429, 503}:
            retry_at = self._retry_at(headers=retained, now=now)
            self._blocked_until[validated.origin] = retry_at
            return FetchResult(
                status=FetchStatus.RETRY_LATER,
                url=validated.safe_url,
                network_status_code=status_code,
                headers=retained,
                validated_at=now,
                next_request_at=retry_at,
                error_code="remote_retry_later",
                message="The origin asked the client to retry later.",
            )

        if 200 <= status_code < 300:
            expires_at, cacheable = self._expiration(now=now, headers=retained)
            entry = CachedLifecycleResponse(
                key=validated.cache_key,
                url=validated.safe_url,
                status_code=status_code,
                headers=retained,
                fetched_at=now,
                validated_at=now,
                expires_at=expires_at,
            )
            if cacheable:
                self._cache.put(entry)
            return FetchResult(
                status=FetchStatus.SUCCESS,
                url=validated.safe_url,
                status_code=status_code,
                network_status_code=status_code,
                headers=retained,
                fetched_at=now,
                validated_at=now,
                expires_at=expires_at if cacheable else None,
            )

        return FetchResult(
            status=FetchStatus.HTTP_ERROR,
            url=validated.safe_url,
            network_status_code=status_code,
            headers=retained,
            validated_at=now,
            error_code="http_error",
            message="The origin returned an unsuccessful HTTP status.",
        )

    def _result_from_cache(
        self,
        *,
        status: FetchStatus,
        validated: _ValidatedUrl,
        cached: CachedLifecycleResponse,
        now: datetime,
        next_request_at: datetime | None = None,
    ) -> FetchResult:
        return FetchResult(
            status=status,
            url=validated.safe_url,
            status_code=cached.status_code,
            headers=cached.headers,
            fetched_at=cached.fetched_at,
            validated_at=cached.validated_at,
            expires_at=cached.expires_at,
            next_request_at=next_request_at,
            stale=cached.expires_at <= now,
            error_code="origin_rate_limited" if status is FetchStatus.RATE_LIMITED else None,
            message=(
                "A stale cached result is available while the origin is rate limited."
                if status is FetchStatus.RATE_LIMITED
                else None
            ),
        )

    def _validate_url(self, url: str) -> _ValidatedUrl:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise _PolicyViolation("invalid_url", "The request URL is invalid.") from exc

        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"}:
            raise _PolicyViolation("unsupported_scheme", "Only HTTP(S) URLs are supported.")
        if parsed.username is not None or parsed.password is not None:
            raise _PolicyViolation("userinfo_forbidden", "URL user information is forbidden.")
        if parsed.fragment:
            raise _PolicyViolation("fragment_forbidden", "URL fragments are forbidden.")
        if parsed.hostname is None:
            raise _PolicyViolation("missing_host", "The request URL must include a host.")

        host = _normalize_hostname(parsed.hostname)
        if not self._host_is_allowed(host):
            raise _PolicyViolation("host_not_allowed", "The request host is not allowlisted.")

        loopback = _is_loopback_host(host)
        if loopback and not self._allow_loopback:
            raise _PolicyViolation(
                "loopback_forbidden",
                "Loopback targets require explicit test-mode permission.",
            )
        if scheme == "http" and not (self._allow_loopback and loopback):
            raise _PolicyViolation(
                "https_required",
                "Plain HTTP is allowed only for explicitly enabled loopback fixtures.",
            )

        request_url = str(httpx.URL(url))
        origin_port = port if port is not None else (443 if scheme == "https" else 80)
        origin = f"{scheme}://{_display_host(host)}:{origin_port}"
        cache_key = hashlib.sha256(request_url.encode("utf-8")).hexdigest()
        return _ValidatedUrl(
            request_url=request_url,
            safe_url=redact_query_values(request_url),
            cache_key=cache_key,
            origin=origin,
        )

    def _host_is_allowed(self, host: str) -> bool:
        if host in self._exact_hosts:
            return True
        return any(
            host != suffix and host.endswith(f".{suffix}") for suffix in self._wildcard_suffixes
        )

    def _request_headers(
        self,
        headers: Mapping[str, str] | None,
        *,
        cached: CachedLifecycleResponse | None,
    ) -> dict[str, str]:
        result: dict[str, str] = {
            "accept": "*/*",
            "user-agent": "sunset-sentinel-api/0.1",
        }
        if headers is not None:
            for name, value in headers.items():
                normalized = name.casefold()
                if normalized in _FORBIDDEN_REQUEST_OVERRIDES | _CONDITIONAL_HEADERS:
                    continue
                result[name] = value
        if cached is not None:
            etag = _last_header(cached.headers, "etag")
            modified = _last_header(cached.headers, "last-modified")
            if etag is not None:
                result["if-none-match"] = etag
            if modified is not None:
                result["if-modified-since"] = modified
        return result

    def _next_request_at(self, origin: str) -> datetime | None:
        candidates: list[datetime] = []
        last_request = self._last_request_at.get(origin)
        if last_request is not None:
            candidates.append(last_request + self._minimum_origin_interval)
        blocked_until = self._blocked_until.get(origin)
        if blocked_until is not None:
            candidates.append(blocked_until)
        return max(candidates) if candidates else None

    def _expiration(self, *, now: datetime, headers: HeaderTuple) -> tuple[datetime, bool]:
        directives = _cache_control_directives(headers)
        if "no-store" in directives:
            return now, False
        if "no-cache" in directives:
            return now, True

        ttl = self._default_ttl
        max_age = directives.get("max-age")
        if max_age is not None:
            try:
                seconds = max(0, int(max_age.strip('"')))
            except ValueError:
                seconds = int(self._default_ttl.total_seconds())
            ttl = timedelta(seconds=seconds)
        ttl = min(ttl, self._maximum_ttl)
        return now + ttl, True

    def _retry_at(self, *, headers: HeaderTuple, now: datetime) -> datetime:
        raw_value = _last_header(headers, "retry-after")
        delay = self._retry_after_fallback
        if raw_value is not None:
            normalized = raw_value.strip()
            if normalized.isascii() and normalized.isdigit():
                delay = timedelta(seconds=int(normalized))
            else:
                try:
                    parsed = parse_http_date(
                        normalized,
                        now=now,
                        allow_obsolete=True,
                        allow_utc_alias=True,
                    ).value
                except ValueError:
                    pass
                else:
                    delay = max(timedelta(0), parsed - now)
        return now + min(delay, self._maximum_retry_after)


def _utc_now(clock: Clock) -> datetime:
    value = clock.now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock.now() must return a timezone-aware datetime")
    return value.astimezone(UTC).replace(microsecond=0)


def _normalize_allowlist_entry(entry: str) -> tuple[str, bool]:
    value = entry.strip().rstrip(".")
    wildcard = value.startswith("*.")
    host = value[2:] if wildcard else value
    if not host or "*" in host or "://" in host or "/" in host or "@" in host:
        raise ValueError(f"invalid allowlist host pattern: {entry!r}")
    return _normalize_hostname(host), wildcard


def _normalize_hostname(host: str) -> str:
    normalized = host.rstrip(".").casefold()
    try:
        return ipaddress.ip_address(normalized).compressed
    except ValueError:
        try:
            return normalized.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise _PolicyViolation("invalid_host", "The request host is invalid.") from exc


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _display_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def redact_query_values(url: str) -> str:
    """Return a display URL that omits credentials and masks every query value."""

    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<invalid-url>"
    if host is None:
        return "<invalid-url>"

    try:
        normalized_host = _normalize_hostname(host)
    except _PolicyViolation:
        return "<invalid-url>"
    netloc = _display_host(normalized_host)
    if port is not None:
        netloc = f"{netloc}:{port}"

    redacted_query = ""
    if parsed.query:
        components: list[str] = []
        for component in parsed.query.split("&"):
            key, separator, _value = component.partition("=")
            components.append(f"{key}=REDACTED" if separator or key else "REDACTED")
        redacted_query = "&".join(components)
    safe = SplitResult(
        scheme=parsed.scheme.casefold(),
        netloc=netloc,
        path=parsed.path,
        query=redacted_query,
        fragment="",
    )
    return urlunsplit(safe)


def _retained_headers(headers: httpx.Headers) -> HeaderTuple:
    return tuple(
        (
            name.casefold(),
            _redact_link_header(value) if name.casefold() == "link" else value,
        )
        for name, value in headers.multi_items()
        if name.casefold() in _RETAINED_RESPONSE_HEADERS
    )


def _redact_link_header(value: str) -> str:
    def replace_target(match: re.Match[str]) -> str:
        target = match.group(1)
        parsed = urlsplit(target)
        if parsed.hostname is not None:
            return f"<{redact_query_values(target)}>"
        redacted_query = ""
        if parsed.query:
            redacted_query = "&".join(
                f"{component.partition('=')[0]}=REDACTED" for component in parsed.query.split("&")
            )
        safe_relative = urlunsplit(
            SplitResult(
                scheme="",
                netloc="",
                path=parsed.path,
                query=redacted_query,
                fragment="",
            )
        )
        return f"<{safe_relative}>"

    return _LINK_TARGET_RE.sub(replace_target, value)


def _last_header(headers: HeaderTuple, name: str) -> str | None:
    normalized = name.casefold()
    values = [value for field, value in headers if field == normalized]
    return values[-1] if values else None


def _merge_headers(cached: HeaderTuple, fresh: HeaderTuple) -> HeaderTuple:
    replaced_names = {name for name, _value in fresh}
    return tuple(item for item in cached if item[0] not in replaced_names) + fresh


def _cache_control_directives(headers: HeaderTuple) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for raw_value in (value for name, value in headers if name == "cache-control"):
        for raw_directive in raw_value.split(","):
            name, separator, value = raw_directive.strip().partition("=")
            if name:
                result[name.casefold()] = value.strip() if separator else None
    return result
