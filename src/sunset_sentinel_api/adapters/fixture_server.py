"""Deterministic loopback fixture server for lifecycle scanner tests and demos."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import format_datetime

import uvicorn
from fastapi import FastAPI, Header, Response

_ORDERS_DEPRECATION = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
_ORDERS_SUNSET = datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)
_SEARCH_SUNSET = datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC)
_CONFLICT_DEPRECATION = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
_CONFLICT_SUNSET = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)


def _structured_date(value: datetime) -> str:
    return f"@{int(value.timestamp())}"


def _http_date(value: datetime) -> str:
    return format_datetime(value, usegmt=True)


def create_fixture_app() -> FastAPI:
    """Create three deterministic API lifecycle scenarios."""

    app = FastAPI(
        title="Sunset Sentinel fixture API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/v1/orders")
    def orders(if_none_match: str | None = Header(default=None)) -> Response:
        headers = {
            "Deprecation": _structured_date(_ORDERS_DEPRECATION),
            "Sunset": _http_date(_ORDERS_SUNSET),
            "Link": '</migration/orders>; rel="deprecation"; type="text/html"',
            "Cache-Control": "public, max-age=60",
            "ETag": '"orders-lifecycle-v1"',
        }
        if if_none_match == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        return Response(
            content='{"scenario":"rfc-dates","endpoint":"/v1/orders"}',
            media_type="application/json",
            headers=headers,
        )

    @app.get("/v1/search")
    def search() -> Response:
        return Response(
            content='{"scenario":"legacy-boolean","endpoint":"/v1/search"}',
            media_type="application/json",
            headers={
                "Deprecation": "true",
                "Sunset": _http_date(_SEARCH_SUNSET),
                "Link": '</migration/search>; rel="deprecation"; type="text/html"',
                "Cache-Control": "public, max-age=60",
            },
        )

    @app.get("/v1/conflict")
    def conflict() -> Response:
        return Response(
            content='{"scenario":"date-conflict","endpoint":"/v1/conflict"}',
            media_type="application/json",
            headers={
                "Deprecation": _structured_date(_CONFLICT_DEPRECATION),
                "Sunset": _http_date(_CONFLICT_SUNSET),
                "Link": '</migration/conflict>; rel="sunset"; type="text/html"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/migration/{name}")
    def migration(name: str) -> dict[str, str]:
        return {
            "scenario": name,
            "notice": "Synthetic fixture documentation; the scanner never follows this link.",
        }

    return app


app = create_fixture_app()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the loopback-only fixture server."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("the fixture server is intentionally restricted to loopback")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
