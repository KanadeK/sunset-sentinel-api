"""Strict local configuration model."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sunset_sentinel_api.domain import HeaderMode

_HOST_PATTERN = re.compile(
    r"^(?:\*\.)?(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class SentinelConfig(BaseModel):
    """Configuration for local storage, requests, scheduling, and serving."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    database_path: Path = Path("sunset-sentinel.db")
    allowed_hosts: tuple[str, ...] = ()
    cache_ttl_seconds: int = Field(default=3600, ge=60, le=21_600)
    min_request_interval_seconds: int = Field(default=60, ge=1, le=86_400)
    request_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    header_mode: HeaderMode = HeaderMode.COMPAT
    allow_loopback: bool = False
    scan_interval_minutes: int = Field(default=360, ge=1, le=10_080)
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8000, ge=1, le=65_535)

    @field_validator("allowed_hosts")
    @classmethod
    def validate_allowed_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            host = value.strip().rstrip(".").lower()
            if not host or not _HOST_PATTERN.fullmatch(host):
                raise ValueError(f"invalid allowed host pattern: {value!r}")
            if host not in normalized:
                normalized.append(host)
        return tuple(sorted(normalized))

    @field_validator("bind_host")
    @classmethod
    def restrict_bind_host(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("v0.1.0 serves only on loopback interfaces")
        return value

    @classmethod
    def from_json_file(cls, path: Path) -> SentinelConfig:
        """Load a UTF-8 JSON configuration file with strict fields."""

        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("configuration root must be a JSON object")
        return cls.model_validate(raw)
