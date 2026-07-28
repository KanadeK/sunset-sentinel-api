"""Build, verify, and safely publish the complete local release asset set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import uuid
import venv
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

from sunset_sentinel_api import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "dist-release"
PROJECT_SLUG = "sunset-sentinel-api"
OWNERSHIP_MARKER = ".sunset-sentinel-release-output"
ARCHIVE_TIMESTAMP = (2026, 7, 23, 0, 0, 0)
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


def _run(
    command: list[str],
    *,
    cwd: Path = PROJECT_ROOT,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"$ {shlex.join(command)}", flush=True)
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            capture_output=capture,
        )
    except subprocess.CalledProcessError as exc:
        if capture and exc.stderr:
            print(exc.stderr, file=sys.stderr, end="")
        raise


def _project_version() -> str:
    loaded = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = loaded.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml is missing [project]")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml is missing project.version")
    if version != __version__:
        raise ValueError(f"version mismatch: pyproject={version!r}, runtime={__version__!r}")
    return version


def _resolve_output(path: Path) -> Path:
    resolved = path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    if resolved == PROJECT_ROOT or not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError("release output must be a dedicated directory inside the repository")
    return resolved


def _validate_existing_output(output: Path) -> None:
    if not output.exists():
        return
    if not output.is_dir():
        raise ValueError(f"release output is not a directory: {output}")
    entries = list(output.iterdir())
    if entries and not (output / OWNERSHIP_MARKER).is_file():
        raise ValueError(
            f"refusing to replace unowned non-empty directory: {output}; "
            f"expected {OWNERSHIP_MARKER}"
        )


def _safe_rmtree(path: Path, *, expected_parent: Path) -> None:
    resolved = path.resolve()
    if (
        resolved == PROJECT_ROOT
        or not resolved.is_relative_to(PROJECT_ROOT)
        or resolved.parent != expected_parent.resolve()
    ):
        raise ValueError(f"refusing recursive removal outside the staging boundary: {path}")
    shutil.rmtree(resolved)


def _publish_staging(staging: Path, output: Path) -> None:
    backup: Path | None = None
    if output.exists():
        if any(output.iterdir()) and not (output / OWNERSHIP_MARKER).is_file():
            raise ValueError(f"release directory lost its ownership marker: {output}")
        backup = output.parent / f".{output.name}.previous-{uuid.uuid4().hex}"
        os.replace(output, backup)
    try:
        os.replace(staging, output)
    except BaseException:
        if backup is not None and backup.exists() and not output.exists():
            os.replace(backup, output)
        raise
    if backup is not None:
        _safe_rmtree(backup, expected_parent=output.parent)


def _source_paths() -> list[Path]:
    fixed = [
        PROJECT_ROOT / ".dockerignore",
        PROJECT_ROOT / "Dockerfile",
        PROJECT_ROOT / "LICENSE",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "docker-compose.yml",
        PROJECT_ROOT / "pyproject.toml",
    ]
    nested = [
        path
        for directory in (PROJECT_ROOT / "src", PROJECT_ROOT / "examples")
        for path in directory.rglob("*")
        if path.is_file()
        and _archive_member_problem(path.relative_to(PROJECT_ROOT).as_posix()) is None
    ]
    paths = sorted((*fixed, *nested), key=lambda item: item.relative_to(PROJECT_ROOT).as_posix())
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"compose source input must be a regular file: {path}")
    return paths


def _archive_member_problem(name: str) -> str | None:
    path = PurePosixPath(name.replace("\\", "/"))
    lowered = tuple(part.casefold() for part in path.parts)
    if path.is_absolute() or ".." in path.parts:
        return "an unsafe path"
    if any(part in FORBIDDEN_ARCHIVE_PARTS for part in lowered):
        return "cache or workspace metadata"
    basename = path.name.casefold()
    if basename in {".coverage", ".ds_store", "thumbs.db"} or basename.endswith(
        (".pyc", ".pyo", ".tmp", ".db-shm", ".db-wal")
    ):
        return "a cache or temporary file"
    return None


def _validate_zip_archive(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        files = [info for info in archive.infolist() if not info.is_dir()]
        if not files:
            raise ValueError(f"archive contains no files: {path.name}")
        for info in files:
            problem = _archive_member_problem(info.filename)
            if problem is not None:
                raise ValueError(f"{path.name} contains {problem}: {info.filename}")


def _validate_tar_archive(path: Path) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        files = [member for member in members if member.isfile()]
        if not files:
            raise ValueError(f"archive contains no files: {path.name}")
        if any(member.issym() or member.islnk() for member in members):
            raise ValueError(f"{path.name} contains a symbolic or hard link")
        for member in files:
            problem = _archive_member_problem(member.name)
            if problem is not None:
                raise ValueError(f"{path.name} contains {problem}: {member.name}")


def _write_source_bundle(path: Path, version: str) -> None:
    prefix = f"{PROJECT_SLUG}-{version}"
    with zipfile.ZipFile(
        path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for source in _source_paths():
            relative = source.relative_to(PROJECT_ROOT).as_posix()
            info = zipfile.ZipInfo(f"{prefix}/{relative}", date_time=ARCHIVE_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())


def _venv_python(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_console(directory: Path) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return directory / ("Scripts" if os.name == "nt" else "bin") / (f"sunset-sentinel{suffix}")


def _verify_wheel(wheel: Path, version: str, temporary_root: Path) -> str:
    environment = temporary_root / "wheel-venv"
    venv.EnvBuilder(
        with_pip=True,
        clear=True,
        system_site_packages=False,
    ).create(environment)
    python = _venv_python(environment)
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            str(wheel),
        ]
    )
    console = _venv_console(environment)
    if not console.is_file():
        raise RuntimeError(f"wheel did not install the console entry point: {console}")
    completed = _run([str(console), "--version"], capture=True)
    reported = completed.stdout.strip()
    if reported != version:
        raise RuntimeError(f"installed wheel reported {reported!r}; expected version {version!r}")

    demo_database = temporary_root / "installed-wheel-demo.sqlite"
    demo_output = temporary_root / "installed-wheel-demo"
    completed = _run(
        [
            str(console),
            "demo",
            "--database",
            str(demo_database),
            "--output-dir",
            str(demo_output),
        ],
        capture=True,
    )
    expected = {
        "assessment.json",
        "issue-drafts.json",
        "lifecycle.ics",
        "migration-checklist.md",
        "report.md",
    }
    generated = {path.name for path in demo_output.iterdir() if path.is_file()}
    if generated != expected:
        raise RuntimeError(
            f"installed wheel demo generated {sorted(generated)}; expected {sorted(expected)}"
        )
    assessment = json.loads((demo_output / "assessment.json").read_text(encoding="utf-8"))
    if not isinstance(assessment, dict) or len(assessment.get("records", [])) != 3:
        raise RuntimeError("installed wheel demo did not assess three bundled records")
    summary = json.loads(completed.stdout)
    if not isinstance(summary, dict) or summary.get("records") != 3:
        raise RuntimeError("installed wheel demo summary did not report three records")
    return reported


def _copy_all(paths: Iterable[Path], destination: Path) -> list[Path]:
    copied: list[Path] = []
    for source in paths:
        target = destination / source.name
        shutil.copy2(source, target)
        copied.append(target)
    return copied


def _write_checksums(staging: Path, assets: Sequence[Path]) -> Path:
    checksum_path = staging / "SHA256SUMS.txt"
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in sorted(assets, key=lambda item: item.name)
    ]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return checksum_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build wheel, sdist, Compose sources, real demo exports, checksums, "
            "and verify the wheel in a temporary environment."
        )
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Build all v0.1.0 release assets and atomically replace owned output."""
    args = build_parser().parse_args(argv)
    version = _project_version()
    output = _resolve_output(args.output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    _validate_existing_output(output)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)).resolve()

    try:
        with tempfile.TemporaryDirectory(prefix="sunset-sentinel-package-") as temporary_name:
            temporary = Path(temporary_name)
            build_output = temporary / "build"
            build_output.mkdir()
            _run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--no-isolation",
                    "--outdir",
                    str(build_output),
                ]
            )
            wheels = sorted(build_output.glob("*.whl"))
            sdists = sorted(build_output.glob("*.tar.gz"))
            if len(wheels) != 1 or len(sdists) != 1:
                raise RuntimeError(
                    f"expected one wheel and one sdist, got {len(wheels)} and {len(sdists)}"
                )
            _validate_zip_archive(wheels[0])
            _validate_tar_archive(sdists[0])
            copied = _copy_all((*wheels, *sdists), staging)

            compose_bundle = staging / f"{PROJECT_SLUG}-{version}-compose-source.zip"
            _write_source_bundle(compose_bundle, version)
            _validate_zip_archive(compose_bundle)
            copied.append(compose_bundle)

            demo_output = temporary / "demo"
            _run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "demo.py"),
                    "--output-dir",
                    str(demo_output),
                ]
            )
            report = staging / f"{PROJECT_SLUG}-{version}-demo-report.md"
            calendar = staging / f"{PROJECT_SLUG}-{version}-demo-calendar.ics"
            shutil.copy2(demo_output / "report.md", report)
            shutil.copy2(demo_output / "lifecycle.ics", calendar)
            copied.extend((report, calendar))

            installed_version = _verify_wheel(wheels[0], version, temporary)
            checksum = _write_checksums(staging, copied)
            (staging / OWNERSHIP_MARKER).write_text(
                f"{PROJECT_SLUG} {version}\n",
                encoding="utf-8",
                newline="\n",
            )

        _publish_staging(staging, output)
    except BaseException:
        if staging.exists():
            _safe_rmtree(staging, expected_parent=output.parent)
        raise

    assets = sorted(
        path.name for path in output.iterdir() if path.is_file() and path.name != OWNERSHIP_MARKER
    )
    print(
        json.dumps(
            {
                "assets": assets,
                "checksum_manifest": checksum.name,
                "installed_wheel_version": installed_version,
                "output_dir": str(output),
                "version": version,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
