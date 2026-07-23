"""FastAPI application and local web dashboard for Sunset Sentinel."""

import hashlib
import json
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, cast

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint

from sunset_sentinel_api import __version__
from sunset_sentinel_api.adapters.sqlite_repository import (
    LifecycleChange,
    SQLiteRepository,
)
from sunset_sentinel_api.clock import Clock, SystemClock
from sunset_sentinel_api.domain.models import LifecycleSignal, as_utc
from sunset_sentinel_api.exporters import (
    assessment_to_json,
    build_issue_drafts,
    render_ics_calendar,
    render_markdown_report,
    render_migration_checklist,
)
from sunset_sentinel_api.services.assessment import Assessment
from sunset_sentinel_api.services.monitor import assess_repository, import_file_sources

_WEB_DIRECTORY = Path(__file__).resolve().parent / "web"
_DEFAULT_SAMPLE_DIRECTORY = Path(__file__).resolve().parents[2] / "examples"
_EXPORT_NAMES = frozenset({"json", "markdown", "calendar", "checklist", "issues"})
_MUTATION_HEADER_VALUE = "dashboard-v1"


def create_app(
    *,
    database_path: Path | None = None,
    clock: Clock | None = None,
    sample_dir: Path | None = None,
) -> FastAPI:
    """Create a local dashboard app with request-scoped SQLite connections."""

    selected_clock = clock if clock is not None else SystemClock()
    selected_database = (
        Path(os.environ.get("SUNSET_SENTINEL_DATABASE", "sunset-sentinel.db"))
        if database_path is None
        else Path(database_path)
    )
    selected_samples = (
        Path(
            os.environ.get(
                "SUNSET_SENTINEL_SAMPLE_DIR",
                str(_DEFAULT_SAMPLE_DIRECTORY),
            )
        )
        if sample_dir is None
        else Path(sample_dir)
    )

    app = FastAPI(
        title="Sunset Sentinel API",
        version=__version__,
        description="Local API lifecycle monitoring and migration planning.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def open_repository() -> Iterator[SQLiteRepository]:
        with SQLiteRepository(selected_database, clock=selected_clock) as repository:
            yield repository

    Repository = Annotated[SQLiteRepository, Depends(open_repository)]

    @app.middleware("http")
    async def add_security_headers(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path == "/" or request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health(repository: Repository) -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "database": "ready",
            "signals": len(repository.list_signals()),
            "changes": len(repository.list_changes()),
        }

    @app.get("/api/records")
    def records(repository: Repository) -> dict[str, object]:
        assessment = assess_repository(repository, now=selected_clock.now())
        return _assessment_payload(assessment)

    @app.get("/api/changes")
    def changes(repository: Repository) -> dict[str, object]:
        items = [_change_payload(change) for change in reversed(repository.list_changes())]
        return {
            "generated_at": _iso_datetime(selected_clock.now()),
            "changes": items,
        }

    @app.post("/api/import/sample")
    def import_sample(request: Request, repository: Repository) -> dict[str, object]:
        if request.headers.get("x-sunset-sentinel") != _MUTATION_HEADER_VALUE:
            raise HTTPException(
                status_code=403,
                detail="A local mutation confirmation header is required.",
            )
        required_files = (
            selected_samples / "openapi.yaml",
            selected_samples / "manual-feed.yaml",
            selected_samples / "consumers.json",
        )
        if not all(path.is_file() for path in required_files):
            raise HTTPException(status_code=404, detail="Sample data is unavailable.")
        observed_at = selected_clock.now()
        try:
            summary = import_file_sources(
                repository,
                observed_at=observed_at,
                openapi_files={"fixture-api": required_files[0]},
                manual_feed_files=(required_files[1],),
                consumer_files=(required_files[2],),
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="Sample data could not be imported.",
            ) from exc
        return {
            "imported_at": _iso_datetime(observed_at),
            **summary.model_dump(mode="json"),
            "changes": summary.changes,
        }

    @app.get("/api/export/{export_name}")
    def export(export_name: str, repository: Repository) -> Response:
        if export_name not in _EXPORT_NAMES:
            raise HTTPException(status_code=404, detail="Unknown export format.")
        assessment = assess_repository(repository, now=selected_clock.now())
        content, media_type, filename = _render_export(export_name, assessment)
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(_WEB_DIRECTORY / "index.html", media_type="text/html")

    app.mount(
        "/static",
        StaticFiles(directory=_WEB_DIRECTORY),
        name="static",
    )
    return app


def _assessment_payload(assessment: Assessment) -> dict[str, object]:
    decoded: object = json.loads(assessment_to_json(assessment))
    if not isinstance(decoded, dict):
        raise RuntimeError("assessment exporter returned an invalid JSON root")
    return cast(dict[str, object], decoded)


def _change_payload(change: LifecycleChange) -> dict[str, object]:
    signal = change.current
    return {
        "id": change.change_id,
        "signal_id": hashlib.sha256(change.signal_key.encode("utf-8")).hexdigest()[:16],
        "type": change.change_type,
        "recorded_at": _iso_datetime(change.recorded_at),
        "target_id": signal.target_id,
        "scope": signal.scope.value,
        "endpoint": _endpoint_payload(signal),
        "source": signal.source.value,
        "active": signal.active,
        "deprecated": signal.deprecated,
        "deprecation_at": _optional_iso_datetime(signal.deprecation_at),
        "sunset_at": _optional_iso_datetime(signal.sunset_at),
    }


def _endpoint_payload(signal: LifecycleSignal) -> dict[str, object] | None:
    if signal.endpoint is None:
        return None
    return {
        "method": signal.endpoint.method,
        "path": signal.endpoint.path,
        "operation_id": signal.endpoint.operation_id,
    }


def _render_export(
    export_name: str,
    assessment: Assessment,
) -> tuple[str, str, str]:
    if export_name == "json":
        return assessment_to_json(assessment), "application/json", "sunset-sentinel.json"
    if export_name == "markdown":
        return (
            render_markdown_report(assessment),
            "text/markdown",
            "sunset-sentinel-report.md",
        )
    if export_name == "calendar":
        return (
            render_ics_calendar(assessment),
            "text/calendar",
            "sunset-sentinel-calendar.ics",
        )
    if export_name == "checklist":
        return (
            render_migration_checklist(assessment),
            "text/markdown",
            "sunset-sentinel-checklist.md",
        )
    issues = {
        "generated_at": _iso_datetime(assessment.generated_at),
        "issues": [draft.model_dump(mode="json") for draft in build_issue_drafts(assessment)],
    }
    return (
        json.dumps(issues, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        "application/json",
        "sunset-sentinel-issues.json",
    )


def _iso_datetime(value: datetime) -> str:
    return as_utc(value).isoformat().replace("+00:00", "Z")


def _optional_iso_datetime(value: datetime | None) -> str | None:
    return None if value is None else _iso_datetime(value)


app = create_app()


__all__ = ["app", "create_app"]
