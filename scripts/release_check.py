"""Run the non-negotiable v0.1.0 release-readiness checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from sunset_sentinel_api import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "dist-release"
PROJECT_SLUG = "sunset-sentinel-api"
OWNERSHIP_MARKER = ".sunset-sentinel-release-output"
EMPTY_MARKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("TO" + "DO", re.compile(r"\bTO" + r"DO\b", re.I)),
    ("FIX" + "ME", re.compile(r"\bFIX" + r"ME\b", re.I)),
    (
        "Not" + "Implemented",
        re.compile(r"\bNot" + r"Implemented(?:Error)?\b", re.I),
    ),
    (
        "place" + "holder implementation",
        re.compile(
            r"\bplace" + r"holder\s+(?:content|data|implementation|response|text)\b",
            re.I,
        ),
    ),
    ("coming" + " soon", re.compile(r"\bcoming\s+soon\b", re.I)),
    ("lorem" + " ipsum", re.compile(r"\blorem\s+ipsum\b", re.I)),
)
EMPTY_MARKER_EXEMPT_FILES = frozenset({"docs/RELEASE_CHECKLIST.md", "scripts/release_check.py"})
FORBIDDEN_ARCHIVE_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
    }
)
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "GitHub fine-grained token": re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "AWS access key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def _run(
    command: list[str],
    *,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"$ {shlex.join(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=True,
        text=True,
        capture_output=capture,
    )


def _git(*arguments: str) -> str:
    return _run(["git", *arguments]).stdout


def _version() -> str:
    loaded = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = loaded.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml is missing [project]")
    version = project.get("version")
    if not isinstance(version, str):
        raise ValueError("pyproject.toml is missing project.version")
    if version != __version__:
        raise ValueError(f"pyproject version {version!r} differs from runtime {__version__!r}")
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    if f"image: {PROJECT_SLUG}:{version}" not in compose:
        raise ValueError("docker-compose.yml image tag does not match project version")
    return f"pyproject, runtime, and Compose agree on {version}"


def _clean_worktree() -> str:
    status = _git("status", "--porcelain=v1", "--untracked-files=all")
    if status.strip():
        preview = " | ".join(status.splitlines()[:8])
        raise ValueError(f"worktree is not clean: {preview}")
    return "tracked and untracked status is clean"


def _changelog() -> str:
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version = re.escape(__version__)
    heading = re.compile(
        rf"^##\s+\[?v?{version}\]?(?:\s+-\s+\d{{4}}-\d{{2}}-\d{{2}})?\s*$",
        re.MULTILINE,
    )
    if heading.search(changelog) is None:
        raise ValueError(f"CHANGELOG.md has no release heading for v{__version__}")
    return f"CHANGELOG.md contains the v{__version__} release heading"


def _resolve_output(path: Path) -> Path:
    resolved = path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    if resolved == PROJECT_ROOT or not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError("release assets must be inside a dedicated repository directory")
    return resolved


def _asset_files(output: Path) -> list[Path]:
    if not output.is_dir():
        raise ValueError(f"release output does not exist: {output}")
    if not (output / OWNERSHIP_MARKER).is_file():
        raise ValueError(f"release output is missing {OWNERSHIP_MARKER}")
    files = sorted(
        (path for path in output.iterdir() if path.is_file() and path.name != OWNERSHIP_MARKER),
        key=lambda item: item.name,
    )
    names = {path.name for path in files}
    wheels = [name for name in names if name.endswith(".whl")]
    sdists = [name for name in names if name.endswith(".tar.gz")]
    required = {
        f"{PROJECT_SLUG}-{__version__}-compose-source.zip",
        f"{PROJECT_SLUG}-{__version__}-demo-calendar.ics",
        f"{PROJECT_SLUG}-{__version__}-demo-report.md",
        "SHA256SUMS.txt",
    }
    if len(wheels) != 1 or len(sdists) != 1 or not required.issubset(names):
        raise ValueError(
            "release assets must contain one wheel, one sdist, Compose sources, "
            "demo report/calendar, and SHA256SUMS.txt"
        )
    return files


def _assets_and_checksums(output: Path) -> str:
    files = _asset_files(output)
    manifest_path = output / "SHA256SUMS.txt"
    manifest: dict[str, str] = {}
    for line_number, line in enumerate(
        manifest_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        parts = line.split("  ", maxsplit=1)
        if (
            len(parts) != 2
            or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None
            or Path(parts[1]).name != parts[1]
            or parts[1] in manifest
        ):
            raise ValueError(f"invalid checksum manifest line {line_number}")
        manifest[parts[1]] = parts[0]

    assets = [path for path in files if path.name != manifest_path.name]
    if set(manifest) != {path.name for path in assets}:
        raise ValueError("checksum manifest does not exactly cover the release assets")
    for path in assets:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if manifest[path.name] != digest:
            raise ValueError(f"checksum mismatch for {path.name}")
    return f"{len(assets)} assets have exact SHA-256 coverage"


def _archive_member_problem(name: str) -> str | None:
    path = PurePosixPath(name.replace("\\", "/"))
    lowered = tuple(part.casefold() for part in path.parts)
    if path.is_absolute() or ".." in path.parts:
        return "unsafe path"
    if any(part in FORBIDDEN_ARCHIVE_PARTS for part in lowered):
        return "cache or workspace metadata"
    basename = path.name.casefold()
    if basename in {".coverage", ".ds_store", "thumbs.db"} or basename.endswith(
        (".pyc", ".pyo", ".tmp", ".db-shm", ".db-wal")
    ):
        return "cache or temporary file"
    return None


def _archive_hygiene(output: Path) -> str:
    archives = [
        path
        for path in _asset_files(output)
        if path.suffix in {".whl", ".zip"} or path.name.endswith(".tar.gz")
    ]
    if len(archives) != 3:
        raise ValueError("expected wheel, sdist, and Compose source archives")
    for path in archives:
        if path.name.endswith(".tar.gz"):
            with tarfile.open(path, mode="r:gz") as archive:
                members = archive.getmembers()
                if any(member.issym() or member.islnk() for member in members):
                    raise ValueError(f"{path.name} contains an archive link")
                names = [member.name for member in members if member.isfile()]
        else:
            with zipfile.ZipFile(path) as archive:
                infos = [info for info in archive.infolist() if not info.is_dir()]
                if any((info.external_attr >> 16) & 0o170000 == 0o120000 for info in infos):
                    raise ValueError(f"{path.name} contains an archive link")
                names = [info.filename for info in infos]
        if not names:
            raise ValueError(f"{path.name} contains no files")
        for name in names:
            problem = _archive_member_problem(name)
            if problem is not None:
                raise ValueError(f"{path.name} contains {problem}: {name}")
    return "wheel, sdist, and Compose bundle contain no cache or temporary members"


def _tracked_files() -> list[Path]:
    raw = _run(["git", "ls-files", "-z"]).stdout
    return [PROJECT_ROOT / name for name in raw.split("\0") if name]


def _secret_scan() -> str:
    tracked = _tracked_files()
    leaked_env = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in tracked
        if path.name.startswith(".env") and path.name != ".env.example"
    ]
    if leaked_env:
        raise ValueError(f"tracked environment file: {', '.join(leaked_env)}")
    for path in tracked:
        content = path.read_bytes()
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content) is not None:
                relative = path.relative_to(PROJECT_ROOT).as_posix()
                raise ValueError(f"{label} signature found in {relative}")
    return f"{len(tracked)} tracked files passed high-confidence secret signatures"


def _empty_marker_scan() -> str:
    findings: list[str] = []
    for path in _tracked_files():
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative in EMPTY_MARKER_EXEMPT_FILES:
            continue
        content = path.read_bytes()
        if b"\0" in content:
            continue
        text = content.decode("utf-8", errors="ignore")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in EMPTY_MARKER_PATTERNS:
                if pattern.search(line) is not None:
                    findings.append(f"{relative}:{line_number} ({label})")
    if findings:
        raise ValueError(f"empty-shell marker found at {', '.join(findings[:12])}")
    labels = ", ".join(label for label, _pattern in EMPTY_MARKER_PATTERNS)
    return f"no high-confidence empty implementation markers: {labels}"


def _github_identity() -> tuple[str, str]:
    response = _run(["gh", "api", "user"]).stdout
    loaded = cast(object, json.loads(response))
    if not isinstance(loaded, dict):
        raise ValueError("gh api user returned an invalid response")
    login = loaded.get("login")
    account_id = loaded.get("id")
    public_email = loaded.get("email")
    if not isinstance(login, str) or not isinstance(account_id, int):
        raise ValueError("gh api user did not return login and id")
    email = (
        public_email
        if isinstance(public_email, str) and public_email
        else f"{account_id}+{login}@users.noreply.github.com"
    )
    return login, email


def _provenance() -> str:
    login, expected_email = _github_identity()
    rows = _git("log", "--format=%an%x09%ae%x09%cn%x09%ce").splitlines()
    if not rows:
        raise ValueError("repository has no commits")
    mismatches: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        fields = row.split("\t")
        if len(fields) != 4:
            raise ValueError(f"could not parse git identity row {row_number}")
        author_name, author_email, committer_name, committer_email = fields
        if (
            author_name != login
            or committer_name != login
            or author_email != expected_email
            or committer_email != expected_email
        ):
            mismatches.append(
                f"commit {row_number}: "
                f"{author_name} <{author_email}> / "
                f"{committer_name} <{committer_email}>"
            )
    if mismatches:
        raise ValueError(
            f"history identity differs from {login} <{expected_email}>: "
            + " | ".join(mismatches[:5])
        )
    messages = _git("log", "--format=%B")
    if re.search(r"(?im)^Co-authored-by\s*:", messages) is not None:
        raise ValueError("Co-authored-by trailer found in commit history")
    return f"{len(rows)} commits belong to {login}; no Co-authored-by trailer"


def _quality_gate() -> str:
    _run([sys.executable, str(PROJECT_ROOT / "scripts" / "verify.py")], capture=False)
    return "format, lint, mypy, tests, coverage, and build passed"


def _execute(name: str, check: Callable[[], str]) -> CheckResult:
    try:
        detail = check()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        result = CheckResult(name=name, passed=False, detail=str(exc))
    else:
        result = CheckResult(name=name, passed=True, detail=detail)
    status = "PASS" if result.passed else "FAIL"
    print(f"[{status}] {result.name}: {result.detail}", flush=True)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run every required release gate; no check is advisory."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Return success only when source, history, tests, and assets are releasable."""
    args = build_parser().parse_args(argv)
    output = _resolve_output(args.output_dir)
    checks: tuple[tuple[str, Callable[[], str]], ...] = (
        ("clean worktree", _clean_worktree),
        ("version consistency", _version),
        ("CHANGELOG release", _changelog),
        ("release assets and checksums", lambda: _assets_and_checksums(output)),
        ("release archive hygiene", lambda: _archive_hygiene(output)),
        ("secret scan", _secret_scan),
        ("empty-shell marker scan", _empty_marker_scan),
        ("author and committer provenance", _provenance),
        ("full verification and tests", _quality_gate),
    )
    results = [_execute(name, check) for name, check in checks]
    failures = [result for result in results if not result.passed]
    print(
        json.dumps(
            {
                "failed": [result.name for result in failures],
                "passed": sum(result.passed for result in results),
                "total": len(results),
                "version": __version__,
            },
            sort_keys=True,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
