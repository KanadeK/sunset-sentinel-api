"""Generate the deterministic, offline demo artifacts committed under docs/demo."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sunset_sentinel_api.adapters.sqlite_repository import SQLiteRepository
from sunset_sentinel_api.clock import FrozenClock
from sunset_sentinel_api.exporters import (
    assessment_to_json,
    build_issue_drafts,
    render_ics_calendar,
    render_markdown_report,
    render_migration_checklist,
)
from sunset_sentinel_api.services.assessment import Assessment
from sunset_sentinel_api.services.monitor import import_file_sources

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT_ROOT / "examples"
DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "demo"
DEMO_NOW = datetime(2026, 7, 23, tzinfo=UTC)


def render_artifacts(assessment: Assessment) -> dict[str, str]:
    """Render the five public formats from one real assessment."""
    issue_drafts = [draft.model_dump(mode="json") for draft in build_issue_drafts(assessment)]
    return {
        "assessment.json": assessment_to_json(assessment),
        "issue-drafts.json": (
            json.dumps(issue_drafts, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ),
        "lifecycle.ics": render_ics_calendar(assessment),
        "migration-checklist.md": render_migration_checklist(assessment),
        "report.md": render_markdown_report(assessment),
    }


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _resolve_output(path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate five real Sunset Sentinel artifacts without network access."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Artifact directory (relative paths are resolved from the repository root).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Import bundled sources and atomically publish deterministic demo files."""
    args = build_parser().parse_args(argv)
    output_dir = _resolve_output(args.output_dir)
    source_files = (
        EXAMPLES / "openapi.yaml",
        EXAMPLES / "manual-feed.yaml",
        EXAMPLES / "consumers.json",
    )
    missing = [str(path) for path in source_files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"bundled demo source missing: {', '.join(missing)}")

    with tempfile.TemporaryDirectory(prefix="sunset-sentinel-demo-") as temporary:
        database = Path(temporary) / "demo.sqlite"
        with SQLiteRepository(database, clock=FrozenClock(DEMO_NOW)) as repository:
            summary = import_file_sources(
                repository,
                observed_at=DEMO_NOW,
                openapi_files={"fixture-api": source_files[0]},
                manual_feed_files=(source_files[1],),
                consumer_files=(source_files[2],),
            )
            assessment = repository_assessment(repository)

    artifacts = render_artifacts(assessment)
    for filename, content in sorted(artifacts.items()):
        _atomic_write_text(output_dir / filename, content)

    print(
        json.dumps(
            {
                "generated_at": DEMO_NOW.isoformat().replace("+00:00", "Z"),
                "network_requests": 0,
                "output_dir": str(output_dir),
                "records": len(assessment.entries),
                "ingest": summary.model_dump(mode="json"),
                "files": sorted(artifacts),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def repository_assessment(repository: SQLiteRepository) -> Assessment:
    """Assess the imported demo repository at the fixed scenario time."""
    from sunset_sentinel_api.services.monitor import assess_repository

    return assess_repository(repository, now=DEMO_NOW)


if __name__ == "__main__":
    raise SystemExit(main())
