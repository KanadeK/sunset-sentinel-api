from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sunset_sentinel_api.clock import FrozenClock
from sunset_sentinel_api.config import SentinelConfig


def test_frozen_clock_normalizes_to_utc_seconds() -> None:
    clock = FrozenClock(datetime(2026, 7, 23, 12, 30, 15, 999, tzinfo=timezone(timedelta(hours=8))))

    assert clock.now() == datetime(2026, 7, 23, 4, 30, 15, tzinfo=UTC)


def test_config_normalizes_allowlist_and_rejects_external_binding() -> None:
    config = SentinelConfig(
        allowed_hosts=("API.EXAMPLE.TEST.", "*.example.test", "api.example.test"),
    )

    assert config.allowed_hosts == ("*.example.test", "api.example.test")
    with pytest.raises(ValidationError, match="loopback"):
        SentinelConfig(bind_host="192.0.2.1")


@pytest.mark.parametrize("host", ("", "*example.test", "https://example.test", "bad host"))
def test_config_rejects_invalid_allowlist_entries(host: str) -> None:
    with pytest.raises(ValidationError):
        SentinelConfig(allowed_hosts=(host,))
