"""Benchmark the complete offline import, assessment, and rendering pipeline."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import tempfile
import time
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
from sunset_sentinel_api.services.monitor import (
    IngestSummary,
    assess_repository,
    import_file_sources,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT_ROOT / "examples"
DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "demo" / "benchmark.json"
SCENARIO_NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _render_all(assessment: Assessment) -> dict[str, str]:
    issues = [draft.model_dump(mode="json") for draft in build_issue_drafts(assessment)]
    return {
        "assessment.json": assessment_to_json(assessment),
        "issue-drafts.json": json.dumps(issues, ensure_ascii=False, sort_keys=True),
        "lifecycle.ics": render_ics_calendar(assessment),
        "migration-checklist.md": render_migration_checklist(assessment),
        "report.md": render_markdown_report(assessment),
    }


def _exercise(database: Path) -> tuple[float, IngestSummary, Assessment, dict[str, str]]:
    started = time.perf_counter_ns()
    with SQLiteRepository(database, clock=FrozenClock(SCENARIO_NOW)) as repository:
        summary = import_file_sources(
            repository,
            observed_at=SCENARIO_NOW,
            openapi_files={"fixture-api": EXAMPLES / "openapi.yaml"},
            manual_feed_files=(EXAMPLES / "manual-feed.yaml",),
            consumer_files=(EXAMPLES / "consumers.json",),
        )
        assessment = assess_repository(repository, now=SCENARIO_NOW)
        rendered = _render_all(assessment)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if not all(rendered.values()):
        raise RuntimeError("benchmark pipeline produced an empty artifact")
    return elapsed_ms, summary, assessment, rendered


def _nearest_rank(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
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
        description=(
            "Measure a fresh SQLite import, assessment, and five-format render "
            "using only bundled sources."
        )
    )
    parser.add_argument("--iterations", type=int, default=25)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fixed workload and write machine-readable timing evidence."""
    args = build_parser().parse_args(argv)
    if args.iterations < 1:
        raise ValueError("--iterations must be at least 1")
    if args.warmup < 0:
        raise ValueError("--warmup cannot be negative")

    samples: list[float] = []
    last_summary: IngestSummary | None = None
    last_assessment: Assessment | None = None
    last_rendered: dict[str, str] | None = None
    with tempfile.TemporaryDirectory(prefix="sunset-sentinel-benchmark-") as temporary:
        root = Path(temporary)
        for index in range(args.warmup):
            _exercise(root / f"warmup-{index}.sqlite")
        for index in range(args.iterations):
            elapsed, last_summary, last_assessment, last_rendered = _exercise(
                root / f"iteration-{index}.sqlite"
            )
            samples.append(elapsed)

    if last_summary is None or last_assessment is None or last_rendered is None:
        raise RuntimeError("benchmark did not execute")

    rounded_samples = [round(value, 3) for value in samples]
    payload: dict[str, object] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "scenario_at": SCENARIO_NOW.isoformat().replace("+00:00", "Z"),
        "workload": "fresh SQLite import + assessment + five-format render",
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "machine": platform.machine() or "unknown",
            "processor": platform.processor() or "unknown",
            "logical_cpus": os.cpu_count(),
        },
        "data": {
            "consumers": last_summary.consumers,
            "dependencies": last_summary.dependencies,
            "signals": last_summary.signals,
            "records": len(last_assessment.entries),
            "rendered_formats": len(last_rendered),
            "rendered_bytes": {
                name: len(content.encode("utf-8"))
                for name, content in sorted(last_rendered.items())
            },
        },
        "iterations": args.iterations,
        "warmup_iterations": args.warmup,
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(_nearest_rank(samples, 0.95), 3),
        "samples_ms": rounded_samples,
    }
    output = _resolve_output(args.output)
    _atomic_write_json(output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
