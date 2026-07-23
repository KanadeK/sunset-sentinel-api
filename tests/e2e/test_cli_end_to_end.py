from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
IMPORT_AT = "2026-07-23T00:00:00Z"
REPORT_AT = "2026-07-24T00:00:00Z"


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "sunset_sentinel_api.cli",
            *arguments,
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def record_for(
    records: list[dict[str, object]],
    *,
    target_id: str,
    scope: str,
    path: str | None = None,
) -> dict[str, object]:
    for record in records:
        if record["target_id"] != target_id or record["scope"] != scope:
            continue
        endpoints = record["endpoints"]
        assert isinstance(endpoints, list)
        if path is None or any(
            isinstance(endpoint, dict) and endpoint.get("path") == path for endpoint in endpoints
        ):
            return record
    raise AssertionError(f"record not found: target={target_id}, scope={scope}, path={path}")


def test_cli_import_then_json_and_markdown_reports_use_persisted_real_data(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sentinel.sqlite"
    imported = run_cli(
        "import",
        "--database",
        str(database),
        "--openapi",
        f"fixture-api={EXAMPLES / 'openapi.yaml'}",
        "--feed",
        str(EXAMPLES / "manual-feed.yaml"),
        "--consumers",
        str(EXAMPLES / "consumers.json"),
        "--at",
        IMPORT_AT,
    )

    assert imported.returncode == 0, imported.stderr
    assert imported.stderr == ""
    assert json.loads(imported.stdout) == {
        "consumers": 2,
        "dependencies": 3,
        "discovered": 3,
        "signals": 3,
        "updated": 0,
        "withdrawn": 0,
    }
    assert database.is_file()

    json_report = run_cli(
        "report",
        "--database",
        str(database),
        "--format",
        "json",
        "--at",
        REPORT_AT,
    )
    markdown_report = run_cli(
        "report",
        "--database",
        str(database),
        "--format",
        "markdown",
        "--at",
        REPORT_AT,
    )

    assert json_report.returncode == 0, json_report.stderr
    assert markdown_report.returncode == 0, markdown_report.stderr
    payload = json.loads(json_report.stdout)
    assert payload["generated_at"] == REPORT_AT
    records = payload["records"]
    assert isinstance(records, list)
    assert len(records) == 3

    orders = record_for(
        records,
        target_id="fixture-api",
        scope="endpoint",
        path="/v1/orders",
    )
    assert orders["first_seen_at"] == IMPORT_AT
    assert orders["last_seen_at"] == IMPORT_AT
    assert orders["deprecation_at"] == "2026-06-30T23:59:59Z"
    assert orders["sunset_at"] == "2026-09-30T23:59:59Z"
    assert orders["state"] == "deprecated"
    assert orders["consumers"] == [
        {
            "criticality": "critical",
            "id": "checkout-web",
            "name": "Checkout Web",
        }
    ]
    assert orders["scores"] == {
        "blast_radius": 27,
        "blast_radius_band": "medium",
        "priority": 75,
        "priority_band": "high",
        "urgency": 75,
        "urgency_band": "high",
    }

    search = record_for(
        records,
        target_id="fixture-api",
        scope="endpoint",
        path="/v1/search",
    )
    assert search["state"] == "deprecated_date_unknown"
    assert search["consumers"] == [
        {
            "criticality": "medium",
            "id": "ops-reporter",
            "name": "Operations Reporter",
        }
    ]
    assert search["scores"]["priority"] == 60  # type: ignore[index]

    catalog = record_for(
        records,
        target_id="partner-catalog",
        scope="service",
    )
    assert catalog["deprecation_at"] == "2026-10-01T00:00:00Z"
    assert catalog["sunset_at"] == "2027-01-31T23:59:59Z"
    assert catalog["scores"]["priority"] == 45  # type: ignore[index]
    assert catalog["consumers"] == [
        {
            "criticality": "critical",
            "id": "checkout-web",
            "name": "Checkout Web",
        }
    ]

    markdown = markdown_report.stdout
    assert "# Sunset Sentinel lifecycle report" in markdown
    assert "Records: **3**" in markdown
    assert "fixture-api GET /v1/orders" in markdown
    assert "Sunset: `2026-09-30T23:59:59Z`" in markdown
    assert "urgency **75**, blast radius **27**, priority **75**" in markdown
    assert "Checkout Web (`critical`)" in markdown


@pytest.mark.parametrize("failure_kind", ["missing", "malformed"])
def test_cli_invalid_sources_fail_safely_without_partial_records_or_output(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    database = tmp_path / f"{failure_kind}.sqlite"
    if failure_kind == "missing":
        failing_feed = tmp_path / "missing-feed.json"
    else:
        failing_feed = tmp_path / "malformed-feed.json"
        failing_feed.write_text(
            '{"schema_version": 1, "signals": [{"token": "TOP-SECRET"}',
            encoding="utf-8",
        )

    failed_import = run_cli(
        "import",
        "--database",
        str(database),
        "--openapi",
        f"fixture-api={EXAMPLES / 'openapi.yaml'}",
        "--feed",
        str(failing_feed),
        "--at",
        IMPORT_AT,
    )

    assert failed_import.returncode != 0
    assert failed_import.stdout == ""
    assert failed_import.stderr.startswith("error: ")
    assert "Traceback" not in failed_import.stderr
    assert "TOP-SECRET" not in failed_import.stderr

    empty_report = run_cli(
        "report",
        "--database",
        str(database),
        "--format",
        "json",
        "--at",
        REPORT_AT,
    )
    assert empty_report.returncode == 0, empty_report.stderr
    assert json.loads(empty_report.stdout)["records"] == []

    partial_output = tmp_path / "must-not-exist.json"
    invalid_report = run_cli(
        "report",
        "--database",
        str(database),
        "--format",
        "json",
        "--output",
        str(partial_output),
        "--at",
        "not-an-rfc3339-time",
    )
    assert invalid_report.returncode != 0
    assert invalid_report.stdout == ""
    assert invalid_report.stderr.startswith("error: ")
    assert "Traceback" not in invalid_report.stderr
    assert not partial_output.exists()
