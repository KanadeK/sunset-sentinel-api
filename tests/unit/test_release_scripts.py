from __future__ import annotations

import io
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
from scripts import package_release, release_check

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _write_zip(path: Path, member: str = "package/module.py") -> None:
    with zipfile.ZipFile(path, mode="w") as archive:
        archive.writestr(member, "value = 1\n")


def _write_tar(path: Path, member: str = "package/module.py") -> None:
    content = b"value = 1\n"
    info = tarfile.TarInfo(member)
    info.size = len(content)
    with tarfile.open(path, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(content))


def _release_output(tmp_path: Path) -> Path:
    output = tmp_path / "dist-release"
    output.mkdir()
    (output / release_check.OWNERSHIP_MARKER).write_text(
        "sunset-sentinel-api 0.1.0\n",
        encoding="utf-8",
    )
    _write_zip(output / "sunset_sentinel_api-0.1.0-py3-none-any.whl")
    _write_tar(output / "sunset_sentinel_api-0.1.0.tar.gz")
    _write_zip(output / "sunset-sentinel-api-0.1.0-compose-source.zip")
    (output / "sunset-sentinel-api-0.1.0-demo-calendar.ics").write_text(
        "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
        encoding="utf-8",
    )
    (output / "sunset-sentinel-api-0.1.0-demo-report.md").write_text(
        "# Report\n",
        encoding="utf-8",
    )
    (output / "SHA256SUMS.txt").write_text("", encoding="utf-8")
    return output


def test_package_archive_validation_rejects_cached_members(tmp_path: Path) -> None:
    clean_zip = tmp_path / "clean.whl"
    cached_zip = tmp_path / "cached.whl"
    cached_tar = tmp_path / "cached.tar.gz"
    _write_zip(clean_zip)
    _write_zip(cached_zip, "package/__pycache__/module.cpython-312.pyc")
    _write_tar(cached_tar, "package/.pytest_cache/state")

    package_release._validate_zip_archive(clean_zip)
    with pytest.raises(ValueError, match="cache"):
        package_release._validate_zip_archive(cached_zip)
    with pytest.raises(ValueError, match="cache"):
        package_release._validate_tar_archive(cached_tar)


def test_release_archive_hygiene_checks_every_distributable(tmp_path: Path) -> None:
    output = _release_output(tmp_path)

    assert "no cache" in release_check._archive_hygiene(output)

    _write_zip(
        output / "sunset-sentinel-api-0.1.0-compose-source.zip",
        "bundle/src/__pycache__/api.pyc",
    )
    with pytest.raises(ValueError, match="cache"):
        release_check._archive_hygiene(output)


def test_empty_marker_scan_allows_html_and_policy_examples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = tmp_path / "src" / "web" / "index.html"
    checklist = tmp_path / "docs" / "RELEASE_CHECKLIST.md"
    html.parent.mkdir(parents=True)
    checklist.parent.mkdir(parents=True)
    html.write_text('<input placeholder="Search records">\n', encoding="utf-8")
    checklist.write_text(
        "Search for " + "TO" + "DO and " + "FIX" + "ME before release.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release_check, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(release_check, "_tracked_files", lambda: [html, checklist])

    assert "no high-confidence" in release_check._empty_marker_scan()


def test_empty_marker_scan_rejects_implementation_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir()
    source.write_text(
        "# " + "TO" + "DO: replace " + "place" + "holder implementation\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release_check, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(release_check, "_tracked_files", lambda: [source])

    with pytest.raises(ValueError, match="empty-shell marker"):
        release_check._empty_marker_scan()


def test_pytest_scratch_space_is_not_pinned_inside_repository() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    verify_source = (PROJECT_ROOT / "scripts" / "verify.py").read_text(encoding="utf-8")

    assert "--basetemp" not in addopts
    assert "TemporaryDirectory" in verify_source
    assert '"--basetemp"' in verify_source
