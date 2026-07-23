"""Command-line interface for local lifecycle discovery and reporting."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any

from sunset_sentinel_api import __version__
from sunset_sentinel_api.adapters.file_sources import FileSourceError
from sunset_sentinel_api.adapters.fixture_server import main as fixture_server_main
from sunset_sentinel_api.adapters.http_client import HttpLifecycleClient
from sunset_sentinel_api.adapters.sqlite_http_cache import SQLiteHttpCache
from sunset_sentinel_api.adapters.sqlite_repository import RepositoryError, SQLiteRepository
from sunset_sentinel_api.clock import FrozenClock, SystemClock
from sunset_sentinel_api.domain.enums import HeaderMode
from sunset_sentinel_api.domain.models import as_utc
from sunset_sentinel_api.exporters import (
    assessment_to_json,
    build_issue_drafts,
    render_ics_calendar,
    render_markdown_report,
    render_migration_checklist,
)
from sunset_sentinel_api.services.http_scan import HttpScanTarget, scan_http_target
from sunset_sentinel_api.services.monitor import assess_repository, import_file_sources
from sunset_sentinel_api.services.scheduler import SentinelScheduler, scan_job_id

_DEFAULT_DATABASE = Path("sunset-sentinel.db")
_REPORT_FORMATS = ("json", "markdown", "calendar", "checklist", "issues")


def build_parser() -> argparse.ArgumentParser:
    """Build the complete command-line parser."""

    parser = argparse.ArgumentParser(
        prog="sunset-sentinel",
        description="Monitor API deprecation and sunset signals without sending local data away.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="Initialize an empty local database.")
    _add_database_argument(initialize)

    import_parser = subparsers.add_parser(
        "import",
        help="Import OpenAPI, manual feed, and consumer-map files.",
    )
    _add_database_argument(import_parser)
    import_parser.add_argument(
        "--openapi",
        action="append",
        default=[],
        metavar="TARGET=PATH",
        help="Map one stable target ID to an OpenAPI 3.x file; repeatable.",
    )
    import_parser.add_argument(
        "--feed",
        action="append",
        default=[],
        type=Path,
        help="Manual lifecycle feed; repeatable.",
    )
    import_parser.add_argument(
        "--consumers",
        action="append",
        default=[],
        type=Path,
        help="Local consumer/dependency map; repeatable.",
    )
    _add_timestamp_argument(import_parser)

    scan = subparsers.add_parser(
        "scan-http",
        help="Fetch lifecycle headers from one explicitly allowlisted endpoint.",
    )
    _add_database_argument(scan)
    scan.add_argument("--target-id", required=True, help="Stable provider/service identifier.")
    scan.add_argument("--url", required=True, help="HTTPS endpoint to inspect.")
    scan.add_argument("--method", default="GET", help="Endpoint method used for impact mapping.")
    scan.add_argument("--path", help="Endpoint template; defaults to the URL path.")
    scan.add_argument("--operation-id", help="Optional OpenAPI operation ID.")
    scan.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="Allowed exact host or wildcard domain; repeatable and required.",
    )
    scan.add_argument(
        "--allow-loopback",
        action="store_true",
        help="Permit loopback HTTP only for the bundled fixture server.",
    )
    scan.add_argument(
        "--header-mode",
        choices=tuple(mode.value for mode in HeaderMode),
        default=HeaderMode.COMPAT.value,
    )
    scan.add_argument("--cache-ttl", type=int, default=3600, metavar="SECONDS")
    scan.add_argument("--min-request-interval", type=int, default=60, metavar="SECONDS")
    scan.add_argument("--timeout", type=float, default=10.0, metavar="SECONDS")
    _add_timestamp_argument(scan)

    report = subparsers.add_parser(
        "report",
        help="Assess persisted evidence and render a deterministic export.",
    )
    _add_database_argument(report)
    report.add_argument("--format", choices=_REPORT_FORMATS, default="markdown")
    report.add_argument("--output", type=Path, help="Write to a file instead of standard output.")
    _add_timestamp_argument(report)

    demo = subparsers.add_parser(
        "demo",
        help="Import the bundled offline sample and generate reviewable artifacts.",
    )
    _add_database_argument(demo)
    demo.add_argument("--sample-dir", type=Path, default=Path("examples"))
    demo.add_argument("--output-dir", type=Path, default=Path("demo-output"))
    _add_timestamp_argument(demo, default="2026-07-23T00:00:00Z")

    watch = subparsers.add_parser(
        "watch",
        help="Periodically import local sources and atomically refresh all reports.",
    )
    _add_database_argument(watch)
    watch.add_argument(
        "--openapi",
        action="append",
        default=[],
        metavar="TARGET=PATH",
        help="Map one stable target ID to an OpenAPI 3.x file; repeatable.",
    )
    watch.add_argument(
        "--feed",
        action="append",
        default=[],
        type=Path,
        help="Manual lifecycle feed; repeatable.",
    )
    watch.add_argument(
        "--consumers",
        action="append",
        default=[],
        type=Path,
        help="Local consumer/dependency map; repeatable.",
    )
    watch.add_argument(
        "--interval-minutes",
        type=int,
        default=60,
        metavar="MINUTES",
        help="Minutes between local source refreshes.",
    )
    watch.add_argument(
        "--job-id",
        default="default",
        help="Stable logical ID used to derive the APScheduler job ID.",
    )
    watch.add_argument("--output-dir", type=Path, default=Path("watch-output"))
    watch.add_argument(
        "--once",
        action="store_true",
        help="Run the registered job once synchronously and exit.",
    )

    serve = subparsers.add_parser("serve", help="Serve the local API and Web dashboard.")
    _add_database_argument(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--sample-dir", type=Path, default=Path("examples"))

    fixture = subparsers.add_parser(
        "fixture-server",
        help="Run the deterministic loopback lifecycle fixture.",
    )
    fixture.add_argument("--host", default="127.0.0.1")
    fixture.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a requested command and return a shell-friendly status code."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (FileNotFoundError, FileSourceError, RepositoryError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "init":
        with SQLiteRepository(args.database):
            pass
        print(json.dumps({"database": str(args.database), "initialized": True}))
        return 0
    if args.command == "import":
        return _run_import(args)
    if args.command == "scan-http":
        return _run_http_scan(args)
    if args.command == "report":
        return _run_report(args)
    if args.command == "demo":
        return _run_demo(args)
    if args.command == "watch":
        return _run_watch(args)
    if args.command == "serve":
        return _run_server(args)
    if args.command == "fixture-server":
        return fixture_server_main(["--host", args.host, "--port", str(args.port)])
    raise ValueError(f"unsupported command: {args.command}")


def _run_import(args: argparse.Namespace) -> int:
    observed_at = _timestamp(args.at)
    openapi_files = _openapi_mapping(args.openapi)
    if not openapi_files and not args.feed and not args.consumers:
        raise ValueError("provide at least one --openapi, --feed, or --consumers source")
    with SQLiteRepository(args.database, clock=FrozenClock(observed_at)) as repository:
        summary = import_file_sources(
            repository,
            observed_at=observed_at,
            openapi_files=openapi_files,
            manual_feed_files=tuple(args.feed),
            consumer_files=tuple(args.consumers),
        )
    print(json.dumps(_plain_data(summary), ensure_ascii=False, sort_keys=True))
    return 0


def _run_http_scan(args: argparse.Namespace) -> int:
    if not args.allow_host:
        raise ValueError("--allow-host is required for every HTTP scan")
    observed_at = _timestamp(args.at)
    clock = FrozenClock(observed_at)
    with SQLiteRepository(args.database, clock=clock) as repository:
        cache = SQLiteHttpCache(repository)
        with HttpLifecycleClient(
            allowed_hosts=tuple(args.allow_host),
            clock=clock,
            cache=cache,
            allow_loopback=args.allow_loopback,
            default_ttl_seconds=args.cache_ttl,
            minimum_origin_interval_seconds=args.min_request_interval,
            timeout_seconds=args.timeout,
        ) as client:
            outcome = scan_http_target(
                client=client,
                repository=repository,
                target=HttpScanTarget(
                    target_id=args.target_id,
                    url=args.url,
                    method=args.method,
                    path=args.path,
                    operation_id=args.operation_id,
                ),
                observed_at=observed_at,
                mode=HeaderMode(args.header_mode),
            )
    payload = {
        "status": outcome.fetch.status.value,
        "url": outcome.fetch.url,
        "http_status": outcome.fetch.status_code,
        "persisted": outcome.persisted,
        "signal_key": (
            outcome.parsed_signal.signal_key if outcome.parsed_signal is not None else None
        ),
        "diagnostics": [
            {
                "code": diagnostic.code,
                "severity": diagnostic.severity.value,
                "field": diagnostic.field,
                "message": diagnostic.message,
            }
            for diagnostic in outcome.diagnostics
        ],
        "error_code": outcome.fetch.error_code,
        "message": outcome.fetch.message,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if outcome.fetch.ok else 3


def _run_report(args: argparse.Namespace) -> int:
    now = _timestamp(args.at)
    with SQLiteRepository(args.database, clock=FrozenClock(now)) as repository:
        assessment = assess_repository(repository, now=now)
    rendered = _render_assessment(assessment, args.format)
    _emit(rendered, args.output)
    return 0


def _run_demo(args: argparse.Namespace) -> int:
    now = _timestamp(args.at)
    sample_dir = args.sample_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with SQLiteRepository(args.database, clock=FrozenClock(now)) as repository:
        summary = import_file_sources(
            repository,
            observed_at=now,
            openapi_files={"acme-commerce": sample_dir / "openapi.yaml"},
            manual_feed_files=(sample_dir / "manual-feed.yaml",),
            consumer_files=(sample_dir / "consumers.json",),
        )
        assessment = assess_repository(repository, now=now)

    outputs = _assessment_outputs(assessment)
    for filename, content in outputs.items():
        (output_dir / filename).write_text(content, encoding="utf-8", newline="")
    print(
        json.dumps(
            {
                "database": str(args.database),
                "output_dir": str(output_dir),
                "records": len(assessment.entries),
                "ingest": _plain_data(summary),
                "files": sorted(outputs),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _run_watch(args: argparse.Namespace) -> int:
    openapi_files = _openapi_mapping(args.openapi)
    manual_feed_files = tuple(args.feed)
    consumer_files = tuple(args.consumers)
    if not openapi_files and not manual_feed_files and not consumer_files:
        raise ValueError("provide at least one --openapi, --feed, or --consumers source")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    scheduler = SentinelScheduler(clock=clock)
    scheduler_job_id = scan_job_id(args.job_id)

    def refresh() -> dict[str, object]:
        observed_at = clock.now()
        with SQLiteRepository(args.database, clock=clock) as repository:
            summary = import_file_sources(
                repository,
                observed_at=observed_at,
                openapi_files=openapi_files,
                manual_feed_files=manual_feed_files,
                consumer_files=consumer_files,
            )
            assessment = assess_repository(repository, now=observed_at)

        outputs = _assessment_outputs(assessment)
        for filename in sorted(outputs):
            _atomic_write_text(output_dir / filename, outputs[filename])
        payload: dict[str, object] = {
            "database": str(args.database),
            "files": sorted(outputs),
            "ingest": _plain_data(summary),
            "job_id": scheduler_job_id,
            "logical_job_id": args.job_id,
            "output_dir": str(output_dir),
            "records": len(assessment.entries),
            "run_at": observed_at.isoformat().replace("+00:00", "Z"),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
        return payload

    scheduler.schedule_scan(
        target_id=args.job_id,
        interval_minutes=args.interval_minutes,
        task=refresh,
        start_immediately=True,
    )
    if args.once:
        scheduler.run_now(args.job_id)
        return 0

    scheduler.start()
    try:
        Event().wait()
    except KeyboardInterrupt:
        return 0
    finally:
        scheduler.shutdown(wait=True)
    return 0


def _run_server(args: argparse.Namespace) -> int:
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("v0.1.0 serves only on loopback interfaces")
    if not 1 <= args.port <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    import uvicorn

    from sunset_sentinel_api.api import create_app

    app = create_app(
        database_path=args.database,
        sample_dir=args.sample_dir,
        clock=SystemClock(),
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _render_assessment(assessment: Any, format_name: str) -> str:
    if format_name == "json":
        return assessment_to_json(assessment)
    if format_name == "markdown":
        return render_markdown_report(assessment)
    if format_name == "calendar":
        return render_ics_calendar(assessment)
    if format_name == "checklist":
        return render_migration_checklist(assessment)
    if format_name == "issues":
        return _issues_to_json(assessment)
    raise ValueError(f"unsupported report format: {format_name}")


def _issues_to_json(assessment: Any) -> str:
    payload = [
        {
            "record_id": draft.record_id,
            "title": draft.title,
            "labels": list(draft.labels),
            "body": draft.body,
        }
        for draft in build_issue_drafts(assessment)
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _assessment_outputs(assessment: Any) -> dict[str, str]:
    return {
        "assessment.json": assessment_to_json(assessment),
        "issue-drafts.json": _issues_to_json(assessment),
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
        with suppress(OSError):
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise


def _openapi_mapping(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        target_id, separator, raw_path = value.partition("=")
        if not separator or not target_id or not raw_path:
            raise ValueError("--openapi values must use TARGET=PATH")
        if target_id in result:
            raise ValueError(f"duplicate OpenAPI target: {target_id}")
        result[target_id] = Path(raw_path)
    return result


def _timestamp(value: str | None) -> datetime:
    if value is None:
        return SystemClock().now()
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return as_utc(parsed, field_name="--at")


def _plain_data(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _emit(content: str, output: Path | None) -> None:
    if output is None:
        sys.stdout.write(content)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8", newline="")
    print(json.dumps({"output": str(output), "bytes": len(content.encode("utf-8"))}))


def _add_database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, default=_DEFAULT_DATABASE)


def _add_timestamp_argument(
    parser: argparse.ArgumentParser,
    *,
    default: str | None = None,
) -> None:
    parser.add_argument(
        "--at",
        default=default,
        metavar="RFC3339",
        help="Inject the assessment/observation time; defaults to current UTC.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
