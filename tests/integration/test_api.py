from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from sunset_sentinel_api.adapters.sqlite_repository import SQLiteRepository
from sunset_sentinel_api.api import create_app
from sunset_sentinel_api.clock import FrozenClock
from sunset_sentinel_api.domain import (
    EndpointRef,
    LifecycleSignal,
    ScopeKind,
    SignalSource,
)

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).parents[2]
EXAMPLES = REPOSITORY_ROOT / "examples"
MUTATION_HEADERS = {"X-Sunset-Sentinel": "dashboard-v1"}


def make_client(tmp_path: Path, *, sample_dir: Path = EXAMPLES) -> TestClient:
    app = create_app(
        database_path=tmp_path / "sentinel.sqlite",
        clock=FrozenClock(NOW),
        sample_dir=sample_dir,
    )
    return TestClient(app)


def test_dashboard_and_static_assets_are_served_with_security_headers(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        health = client.get("/api/health")
        page = client.get("/")
        styles = client.get("/static/styles.css")
        script = client.get("/static/app.js")
        docs = client.get("/docs")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "version": "0.1.0",
        "database": "ready",
        "signals": 0,
        "changes": 0,
    }
    assert page.status_code == 200
    assert '<h1 id="page-title">Know what breaks next.</h1>' in page.text
    assert 'id="import-button"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert styles.status_code == 200
    assert script.status_code == 200
    assert docs.status_code == 404
    assert 'requestJson("/api/records")' in script.text
    assert 'requestJson("/api/import/sample"' in script.text


def test_sample_import_populates_real_records_and_idempotent_changes(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        imported = client.post("/api/import/sample", headers=MUTATION_HEADERS)
        records = client.get("/api/records")
        changes = client.get("/api/changes")
        repeated = client.post("/api/import/sample", headers=MUTATION_HEADERS)
        changes_after_repeat = client.get("/api/changes")

    assert imported.status_code == 200
    assert imported.json() == {
        "imported_at": "2026-01-15T12:00:00Z",
        "consumers": 2,
        "dependencies": 3,
        "signals": 3,
        "discovered": 3,
        "updated": 0,
        "withdrawn": 0,
        "changes": 3,
    }
    payload = records.json()
    assert records.status_code == 200
    assert payload["generated_at"] == "2026-01-15T12:00:00Z"
    assert len(payload["records"]) == 3
    assert {record["target_id"] for record in payload["records"]} == {
        "fixture-api",
        "partner-catalog",
    }
    assert len(changes.json()["changes"]) == 3
    assert {change["type"] for change in changes.json()["changes"]} == {"discovered"}
    assert repeated.json()["changes"] == 0
    assert len(changes_after_repeat.json()["changes"]) == 3


def test_exports_are_downloadable_and_unknown_format_is_not_exposed(
    tmp_path: Path,
) -> None:
    expected = {
        "json": ("application/json", "sunset-sentinel.json"),
        "markdown": ("text/markdown", "sunset-sentinel-report.md"),
        "calendar": ("text/calendar", "sunset-sentinel-calendar.ics"),
        "checklist": ("text/markdown", "sunset-sentinel-checklist.md"),
        "issues": ("application/json", "sunset-sentinel-issues.json"),
    }
    with make_client(tmp_path) as client:
        assert client.post("/api/import/sample", headers=MUTATION_HEADERS).status_code == 200
        responses = {name: client.get(f"/api/export/{name}") for name in expected}
        missing = client.get("/api/export/private")

    for name, response in responses.items():
        media_type, filename = expected[name]
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(media_type)
        assert filename in response.headers["content-disposition"]
        assert response.content
    assert responses["calendar"].text.startswith("BEGIN:VCALENDAR\r\n")
    assert responses["issues"].json()["issues"]
    assert missing.status_code == 404
    assert "private" not in missing.text


def test_api_projections_redact_query_values_and_raw_source_references(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sentinel.sqlite"
    endpoint = EndpointRef(
        target_id="private-api",
        method="GET",
        path="/v1/orders",
    )
    signal = LifecycleSignal(
        signal_key="private-api:get-orders:http",
        target_id="private-api",
        source=SignalSource.HTTP_HEADER,
        source_ref="https://api.example.test/v1/orders?token=source-secret",
        scope=ScopeKind.ENDPOINT,
        endpoint=endpoint,
        sunset_at=NOW + timedelta(days=30),
        documentation_url="https://docs.example.test/migrate?token=doc-secret",
        replacement="https://api.example.test/v2?api_key=replacement-secret",
        observed_at=NOW,
        raw_sha256=hashlib.sha256(b"raw-private-source").hexdigest(),
    )
    with SQLiteRepository(database, clock=FrozenClock(NOW)) as repository:
        repository.upsert_signal(signal)

    app = create_app(database_path=database, clock=FrozenClock(NOW), sample_dir=EXAMPLES)
    with TestClient(app) as client:
        records = client.get("/api/records")
        changes = client.get("/api/changes")
        json_export = client.get("/api/export/json")
        issues_export = client.get("/api/export/issues")

    combined = "\n".join((records.text, changes.text, json_export.text, issues_export.text))
    assert records.status_code == 200
    assert changes.status_code == 200
    assert "source-secret" not in combined
    assert "doc-secret" not in combined
    assert "replacement-secret" not in combined
    assert "raw-private-source" not in combined
    assert signal.raw_sha256 not in combined
    assert "token=REDACTED" in combined
    assert "api_key=REDACTED" in combined
    assert signal.signal_key not in changes.text


def test_missing_sample_directory_returns_safe_error(tmp_path: Path) -> None:
    missing_directory = tmp_path / "private" / "missing-samples"
    with make_client(tmp_path, sample_dir=missing_directory) as client:
        response = client.post("/api/import/sample", headers=MUTATION_HEADERS)

    assert response.status_code == 404
    assert response.json() == {"detail": "Sample data is unavailable."}
    assert str(missing_directory) not in response.text


def test_sample_import_rejects_cross_site_simple_post(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/import/sample")

    assert response.status_code == 403
    assert response.json() == {"detail": "A local mutation confirmation header is required."}
