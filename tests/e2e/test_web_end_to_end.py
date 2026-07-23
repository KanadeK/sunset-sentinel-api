from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from sunset_sentinel_api.api import create_app
from sunset_sentinel_api.clock import FrozenClock

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = PROJECT_ROOT / "examples"
MUTATION_HEADERS = {"X-Sunset-Sentinel": "dashboard-v1"}


def _assert_browser_security(response: Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


@pytest.mark.e2e
def test_dashboard_user_can_import_assess_and_download_without_network(
    tmp_path: Path,
) -> None:
    app = create_app(
        database_path=tmp_path / "dashboard-e2e.sqlite",
        clock=FrozenClock(NOW),
        sample_dir=EXAMPLES,
    )

    with TestClient(app) as client:
        dashboard = client.get("/")
        styles = client.get("/static/styles.css")
        script = client.get("/static/app.js")
        health_before = client.get("/api/health")

        imported = client.post("/api/import/sample", headers=MUTATION_HEADERS)
        records = client.get("/api/records")
        changes = client.get("/api/changes")
        json_export = client.get("/api/export/json")
        calendar_export = client.get("/api/export/calendar")
        issues_export = client.get("/api/export/issues")
        unknown_export = client.get("/api/export/not-a-real-format")

        repeated = client.post("/api/import/sample", headers=MUTATION_HEADERS)
        health_after_repeat = client.get("/api/health")
        changes_after_repeat = client.get("/api/changes")

    assert dashboard.status_code == 200
    assert styles.status_code == 200
    assert script.status_code == 200
    _assert_browser_security(dashboard)
    _assert_browser_security(health_before)
    _assert_browser_security(records)
    _assert_browser_security(json_export)

    markup = dashboard.text
    assert '<a class="skip-link" href="#main-content">' in markup
    assert '<main id="main-content" tabindex="-1">' in markup
    assert '<label for="record-search">Search</label>' in markup
    assert '<label for="state-filter">State</label>' in markup
    assert '<label for="sort-records">Order</label>' in markup
    assert 'id="refresh-button"' in markup
    assert 'id="import-button"' in markup
    assert markup.count('type="button"') >= 2
    assert markup.count('aria-live="polite"') >= 3
    assert "@media (max-width: 640px)" in styles.text
    assert "tbody td::before" in styles.text
    assert '"X-Sunset-Sentinel": "dashboard-v1"' in script.text

    assert health_before.status_code == 200
    assert health_before.json() == {
        "status": "ok",
        "version": "0.1.0",
        "database": "ready",
        "signals": 0,
        "changes": 0,
    }

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

    assert records.status_code == 200
    payload = records.json()
    assert payload["generated_at"] == "2026-01-15T12:00:00Z"
    assert len(payload["records"]) == 3
    by_identity = {
        (
            record["target_id"],
            record["endpoints"][0]["path"] if record["endpoints"] else None,
        ): record
        for record in payload["records"]
    }

    orders = by_identity[("fixture-api", "/v1/orders")]
    assert orders["state"] == "deprecation_scheduled"
    assert orders["consumers"] == [
        {
            "id": "checkout-web",
            "name": "Checkout Web",
            "criticality": "critical",
        }
    ]
    assert orders["scores"]["priority"] == 35

    search = by_identity[("fixture-api", "/v1/search")]
    assert search["state"] == "deprecated_date_unknown"
    assert search["consumers"] == [
        {
            "id": "ops-reporter",
            "name": "Operations Reporter",
            "criticality": "medium",
        }
    ]
    assert search["scores"]["priority"] == 60

    catalog = by_identity[("partner-catalog", None)]
    assert catalog["state"] == "deprecation_scheduled"
    assert catalog["consumers"] == [
        {
            "id": "checkout-web",
            "name": "Checkout Web",
            "criticality": "critical",
        }
    ]
    assert catalog["scores"]["priority"] == 26

    assert changes.status_code == 200
    change_payload = changes.json()
    assert change_payload["generated_at"] == "2026-01-15T12:00:00Z"
    assert len(change_payload["changes"]) == 3
    assert {item["type"] for item in change_payload["changes"]} == {"discovered"}
    assert {item["target_id"] for item in change_payload["changes"]} == {
        "fixture-api",
        "partner-catalog",
    }
    assert all(len(item["signal_id"]) == 16 for item in change_payload["changes"])

    assert json_export.status_code == 200
    assert json_export.headers["content-type"].startswith("application/json")
    assert (
        json_export.headers["content-disposition"] == 'attachment; filename="sunset-sentinel.json"'
    )
    assert json_export.json() == payload

    assert calendar_export.status_code == 200
    assert calendar_export.headers["content-type"].startswith("text/calendar")
    assert "sunset-sentinel-calendar.ics" in calendar_export.headers["content-disposition"]
    assert calendar_export.text.startswith("BEGIN:VCALENDAR\r\n")
    assert calendar_export.text.count("BEGIN:VEVENT") == 4
    assert "20260630" in calendar_export.text
    assert "20260930" in calendar_export.text
    assert "20261001" in calendar_export.text
    assert "20270131" in calendar_export.text

    assert issues_export.status_code == 200
    assert issues_export.headers["content-type"].startswith("application/json")
    assert "sunset-sentinel-issues.json" in issues_export.headers["content-disposition"]
    issues = issues_export.json()
    assert issues["generated_at"] == "2026-01-15T12:00:00Z"
    assert len(issues["issues"]) == 3
    assert all(issue["labels"] for issue in issues["issues"])
    assert all("### Migration checklist" in issue["body"] for issue in issues["issues"])
    exported_text = "\n".join((json_export.text, issues_export.text))
    assert "raw_sha256" not in exported_text
    assert "source_ref" not in exported_text

    assert unknown_export.status_code == 404
    assert unknown_export.json() == {"detail": "Unknown export format."}
    assert "not-a-real-format" not in unknown_export.text

    assert repeated.status_code == 200
    assert repeated.json()["changes"] == 0
    assert health_after_repeat.json() == {
        "status": "ok",
        "version": "0.1.0",
        "database": "ready",
        "signals": 3,
        "changes": 3,
    }
    assert changes_after_repeat.json() == change_payload
