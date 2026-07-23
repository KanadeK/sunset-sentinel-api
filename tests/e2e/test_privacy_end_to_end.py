from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FIXED_AT = "2026-07-24T00:00:00Z"


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(_PROJECT_ROOT / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if existing_pythonpath is None
        else os.pathsep.join((source_path, existing_pythonpath))
    )
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "sunset_sentinel_api.cli",
            *arguments,
        ],
        cwd=_PROJECT_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        encoding="utf-8",
        text=True,
        timeout=15,
    )


@pytest.mark.e2e
def test_cli_import_and_every_export_keep_raw_evidence_private(tmp_path: Path) -> None:
    documentation_secret = "documentation-secret-8d31"
    replacement_secret = "replacement-secret-6a42"
    raw_signal_key = "private-feed-signal-key"
    stored_signal_key = f"manual:privacy-api:{raw_signal_key}"
    documentation_url = f"https://docs.example.test/migrate?token={documentation_secret}"
    replacement_url = f"https://api.example.test/v2/orders?api_key={replacement_secret}"
    feed_payload = {
        "schema_version": 1,
        "signals": [
            {
                "signal_key": raw_signal_key,
                "target_id": "privacy-api",
                "scope": "endpoint",
                "method": "GET",
                "path": "/v1/private-orders",
                "deprecated": True,
                "deprecation_at": "2026-08-01T00:00:00Z",
                "sunset_at": "2026-09-01T00:00:00Z",
                "documentation_url": documentation_url,
                "replacement": replacement_url,
            }
        ],
    }
    feed_bytes = (json.dumps(feed_payload, indent=2, sort_keys=True) + "\n").encode()
    raw_sha256 = hashlib.sha256(feed_bytes).hexdigest()
    feed = tmp_path / "manual-feed.json"
    feed.write_bytes(feed_bytes)
    database = tmp_path / "sentinel.db"

    import_result = _run_cli(
        "import",
        "--database",
        str(database),
        "--feed",
        str(feed),
        "--at",
        _FIXED_AT,
    )

    assert import_result.returncode == 0, import_result.stderr
    assert import_result.stderr == ""
    assert json.loads(import_result.stdout)["signals"] == 1
    assert database.is_file()
    assert database.stat().st_size > 0

    filenames = {
        "json": "assessment.json",
        "markdown": "report.md",
        "calendar": "lifecycle.ics",
        "checklist": "migration-checklist.md",
        "issues": "issue-drafts.json",
    }
    report_results: list[subprocess.CompletedProcess[str]] = []
    artifacts: dict[str, str] = {}
    for format_name, filename in filenames.items():
        output = tmp_path / filename
        result = _run_cli(
            "report",
            "--database",
            str(database),
            "--format",
            format_name,
            "--output",
            str(output),
            "--at",
            _FIXED_AT,
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert output.is_file()
        report_results.append(result)
        artifacts[format_name] = output.read_text(encoding="utf-8")

    assessment = json.loads(artifacts["json"])
    assert len(assessment["records"]) == 1
    assert assessment["records"][0]["target_id"] == "privacy-api"
    assert assessment["records"][0]["documentation_urls"] == [
        "https://docs.example.test/migrate?token=REDACTED"
    ]
    assert assessment["records"][0]["replacements"] == [
        "https://api.example.test/v2/orders?api_key=REDACTED"
    ]
    assert "privacy-api GET /v1/private-orders" in artifacts["markdown"]
    assert "BEGIN:VCALENDAR" in artifacts["calendar"]
    assert "privacy-api GET /v1/private-orders" in artifacts["checklist"]
    issues_payload = json.loads(artifacts["issues"])
    assert len(issues_payload) == 1
    issue_body = issues_payload[0]["body"]

    unfolded_calendar = artifacts["calendar"].replace("\r\n ", "").replace("\n ", "")
    assert "token=REDACTED" in artifacts["markdown"]
    assert r"api\_key=REDACTED" in artifacts["markdown"]
    assert "token=REDACTED" in unfolded_calendar
    assert "api_key=REDACTED" in unfolded_calendar
    assert "token=REDACTED" in issue_body
    assert r"api\_key=REDACTED" in issue_body

    privacy_surfaces = {
        **{f"artifact:{name}": content for name, content in artifacts.items()},
        "console:import": import_result.stdout + import_result.stderr,
        **{
            f"console:report:{name}": result.stdout + result.stderr
            for name, result in zip(filenames, report_results, strict=True)
        },
    }
    private_values = (
        documentation_secret,
        replacement_secret,
        raw_sha256,
        raw_signal_key,
        stored_signal_key,
    )
    for surface_name, content in privacy_surfaces.items():
        for private_value in private_values:
            assert private_value not in content, surface_name
        assert "raw_sha256" not in content, surface_name
        assert '"signal_key"' not in content, surface_name


@pytest.mark.e2e
def test_cli_scan_http_blocks_disallowed_host_before_transport(tmp_path: Path) -> None:
    transport_secret = "transport-secret-19f4"
    result = _run_cli(
        "scan-http",
        "--database",
        str(tmp_path / "blocked.db"),
        "--target-id",
        "blocked-api",
        "--url",
        f"https://blocked.example.invalid/v1/status?token={transport_secret}",
        "--allow-host",
        "allowed.example.invalid",
        "--timeout",
        "0.1",
        "--at",
        _FIXED_AT,
    )

    assert result.returncode == 3
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["error_code"] == "host_not_allowed"
    assert payload["http_status"] is None
    assert payload["persisted"] is False
    assert payload["url"] == ("https://blocked.example.invalid/v1/status?token=REDACTED")
    assert transport_secret not in result.stdout
