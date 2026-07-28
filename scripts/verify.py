"""Run the repository's local quality gate."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def run(command: list[str]) -> None:
    """Run one required verification command."""
    print(f"$ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def main() -> int:
    """Run format, lint, type, test, coverage, and build checks."""
    run([sys.executable, "-m", "ruff", "format", "--check", "."])
    run([sys.executable, "-m", "ruff", "check", "."])
    run([sys.executable, "-m", "mypy", "src"])
    with tempfile.TemporaryDirectory(prefix="sunset-sentinel-verify-") as scratch:
        run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--basetemp",
                str(Path(scratch) / "pytest"),
                "--cov=src",
                "--cov-report=term-missing",
                "--cov-fail-under=80",
            ]
        )
    run([sys.executable, "-m", "build", "--no-isolation"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
