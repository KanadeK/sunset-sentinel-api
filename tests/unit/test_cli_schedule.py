from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sunset_sentinel_api.cli import main
from sunset_sentinel_api.services.scheduler import scan_job_id


def test_watch_once_generates_real_artifacts_without_waiting(
    tmp_path: Path,
    capsys: Any,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    sample_dir = project_root / "examples"
    output_dir = tmp_path / "watch-output"
    output_dir.mkdir()
    (output_dir / "report.md").write_text("stale", encoding="utf-8")

    result = main(
        [
            "watch",
            "--database",
            str(tmp_path / "watch.db"),
            "--openapi",
            f"acme-commerce={sample_dir / 'openapi.yaml'}",
            "--feed",
            str(sample_dir / "manual-feed.yaml"),
            "--consumers",
            str(sample_dir / "consumers.json"),
            "--interval-minutes",
            "5",
            "--job-id",
            "sample-watch",
            "--output-dir",
            str(output_dir),
            "--once",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == scan_job_id("sample-watch")
    assert payload["logical_job_id"] == "sample-watch"
    assert payload["records"] >= 3
    assert payload["ingest"]["signals"] >= 3
    assert set(payload["files"]) == {
        "assessment.json",
        "issue-drafts.json",
        "lifecycle.ics",
        "migration-checklist.md",
        "report.md",
    }
    assert json.loads((output_dir / "assessment.json").read_text(encoding="utf-8"))["records"]
    assert "Sunset Sentinel lifecycle report" in (output_dir / "report.md").read_text(
        encoding="utf-8"
    )
    with (output_dir / "lifecycle.ics").open(encoding="utf-8", newline="") as calendar:
        assert "BEGIN:VCALENDAR\r\n" in calendar.read()
    assert "API migration checklist" in (output_dir / "migration-checklist.md").read_text(
        encoding="utf-8"
    )
    assert json.loads((output_dir / "issue-drafts.json").read_text(encoding="utf-8"))
    assert list(output_dir.glob(".*.tmp")) == []


@pytest.mark.parametrize("source_option", [None, "--feed"])
def test_watch_rejects_missing_sources_or_missing_file(
    tmp_path: Path,
    capsys: Any,
    source_option: str | None,
) -> None:
    arguments = [
        "watch",
        "--database",
        str(tmp_path / "watch.db"),
        "--output-dir",
        str(tmp_path / "output"),
        "--once",
    ]
    if source_option is not None:
        arguments.extend([source_option, str(tmp_path / "missing.yaml")])

    result = main(arguments)

    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--interval-minutes", "0"], "interval_minutes"),
        (["--interval-minutes", "525601"], "interval_minutes"),
        (["--job-id", ""], "target_id"),
    ],
)
def test_watch_validates_scheduler_arguments(
    tmp_path: Path,
    capsys: Any,
    extra: list[str],
    message: str,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    arguments = [
        "watch",
        "--database",
        str(tmp_path / "watch.db"),
        "--feed",
        str(project_root / "examples" / "manual-feed.yaml"),
        "--output-dir",
        str(tmp_path / "output"),
        "--once",
        *extra,
    ]

    result = main(arguments)

    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


def test_watch_openapi_mapping_error_happens_before_database_write(
    tmp_path: Path,
    capsys: Any,
) -> None:
    database = tmp_path / "watch.db"

    result = main(
        [
            "watch",
            "--database",
            str(database),
            "--openapi",
            "missing-separator",
            "--once",
        ]
    )

    assert result == 2
    assert not database.exists()
    assert "TARGET=PATH" in capsys.readouterr().err
