from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]


def read_text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def compose_document() -> dict[str, Any]:
    document = yaml.safe_load(read_text("docker-compose.yml"))
    assert isinstance(document, dict)
    return cast(dict[str, Any], document)


def test_dockerfile_is_multistage_minimal_and_non_root() -> None:
    dockerfile = read_text("Dockerfile")
    from_lines = [line.strip() for line in dockerfile.splitlines() if line.startswith("FROM ")]
    copy_lines = [line.strip() for line in dockerfile.splitlines() if line.startswith("COPY ")]

    assert len(from_lines) == 2
    assert all(line.startswith("FROM python:3.12-slim-bookworm") for line in from_lines)
    assert from_lines[0].endswith(" AS builder")
    assert from_lines[1].endswith(" AS runtime")
    assert "USER 10001:10001" in dockerfile
    assert "COPY . " not in dockerfile
    assert "COPY ./" not in dockerfile
    assert copy_lines == [
        "COPY pyproject.toml README.md LICENSE ./",
        "COPY src ./src",
        "COPY --from=builder /opt/venv /opt/venv",
        "COPY --chown=10001:10001 examples /app/examples",
    ]
    assert "pip install --no-cache-dir ." in dockerfile
    assert ".[dev]" not in dockerfile


def test_healthcheck_calls_the_real_local_health_endpoint() -> None:
    dockerfile = read_text("Dockerfile")

    assert "HEALTHCHECK " in dockerfile
    assert "http://127.0.0.1:8000/api/health" in dockerfile
    assert "urllib.request.urlopen" in dockerfile
    assert "timeout=2" in dockerfile
    assert "curl " not in dockerfile
    assert "wget " not in dockerfile


def test_runtime_uses_persistent_database_and_bundled_sample() -> None:
    dockerfile = read_text("Dockerfile")

    assert 'SUNSET_SENTINEL_DATABASE="/data/sunset-sentinel.db"' in dockerfile
    assert 'SUNSET_SENTINEL_SAMPLE_DIR="/app/examples"' in dockerfile
    assert 'VOLUME ["/data"]' in dockerfile
    assert '"uvicorn", "sunset_sentinel_api.api:app"' in dockerfile
    assert '"--host", "0.0.0.0"' in dockerfile
    assert "api.example" not in dockerfile


def test_dockerignore_is_an_explicit_build_context_allowlist() -> None:
    entries = [
        line.strip()
        for line in read_text(".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert entries[0] == "*"
    assert "!src/**" in entries
    assert "!examples/**" in entries
    assert "!pyproject.toml" in entries
    assert "!README.md" in entries
    assert "!LICENSE" in entries
    assert not any("tests" in entry for entry in entries)
    assert not any(".git" in entry for entry in entries)


def test_compose_binds_loopback_and_applies_runtime_hardening() -> None:
    document = compose_document()
    services = cast(dict[str, Any], document["services"])
    service = cast(dict[str, Any], services["sunset-sentinel"])

    assert service["ports"] == ["127.0.0.1:8000:8000"]
    assert service["volumes"] == ["sunset-sentinel-data:/data"]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["tmpfs"] == ["/tmp:size=16m,noexec,nosuid,nodev"]
    assert service["init"] is True
    assert "command" not in service
    assert "secrets" not in document
    assert "secrets" not in service
    assert "depends_on" not in service
    assert "links" not in service

    volumes = cast(dict[str, Any], document["volumes"])
    assert "sunset-sentinel-data" in volumes


def test_compose_environment_contains_configuration_but_no_secret() -> None:
    document = compose_document()
    service = cast(
        dict[str, Any],
        cast(dict[str, Any], document["services"])["sunset-sentinel"],
    )
    environment = cast(dict[str, str], service["environment"])

    assert environment == {
        "SUNSET_SENTINEL_DATABASE": "/data/sunset-sentinel.db",
        "SUNSET_SENTINEL_SAMPLE_DIR": "/app/examples",
        "SUNSET_SENTINEL_PORT": "8000",
    }
    assert not any(
        re.search(r"(password|secret|token|key)", name, flags=re.IGNORECASE) for name in environment
    )
