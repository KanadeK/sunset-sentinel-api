from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sunset_sentinel_api.cli import main


def test_cli_initializes_database(tmp_path: Path, capsys: Any) -> None:
    database = tmp_path / "sentinel.db"

    result = main(["init", "--database", str(database)])

    assert result == 0
    assert database.is_file()
    output = json.loads(_captured_stdout(capsys))
    assert output["initialized"] is True


def test_cli_demo_generates_real_reviewable_artifacts(
    tmp_path: Path,
    capsys: Any,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    output_dir = tmp_path / "demo"

    result = main(
        [
            "demo",
            "--database",
            str(tmp_path / "demo.db"),
            "--sample-dir",
            str(project_root / "examples"),
            "--output-dir",
            str(output_dir),
            "--at",
            "2026-07-23T00:00:00Z",
        ]
    )

    assert result == 0
    summary = json.loads(_captured_stdout(capsys))
    assert summary["records"] >= 3
    assert summary["ingest"]["signals"] >= 3
    assert set(summary["files"]) == {
        "assessment.json",
        "issue-drafts.json",
        "lifecycle.ics",
        "migration-checklist.md",
        "report.md",
    }
    assessment = json.loads((output_dir / "assessment.json").read_text(encoding="utf-8"))
    assert len(assessment["records"]) == summary["records"]
    assert "BEGIN:VCALENDAR" in (output_dir / "lifecycle.ics").read_text(encoding="utf-8")
    assert "API migration checklist" in (output_dir / "migration-checklist.md").read_text(
        encoding="utf-8"
    )


def test_cli_reports_invalid_source_without_traceback(
    tmp_path: Path,
    capsys: Any,
) -> None:
    result = main(
        [
            "import",
            "--database",
            str(tmp_path / "sentinel.db"),
            "--feed",
            str(tmp_path / "missing.yaml"),
            "--at",
            "2026-07-23T00:00:00Z",
        ]
    )

    assert result == 2
    captured = _captured(capsys)
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err


def _captured_stdout(capsys: Any) -> str:
    return _captured(capsys).out


def _captured(capsys: Any) -> Any:
    return capsys.readouterr()
