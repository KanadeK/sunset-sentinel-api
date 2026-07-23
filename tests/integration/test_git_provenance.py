from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sunset_sentinel_api.adapters.git_provenance import (
    GitProvenance,
    GitProvenanceError,
    InvalidGitPathError,
    get_file_provenance,
)


def git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git is required for provenance integration tests")
    return executable


def run_git(
    repository: Path,
    *arguments: str,
    date: str | None = None,
) -> str:
    environment = os.environ.copy()
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    if date is not None:
        environment.update(
            {
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date,
            }
        )
    result = subprocess.run(  # noqa: S603
        [git_executable(), "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    return result.stdout.strip()


def initialize_repository(path: Path) -> None:
    path.mkdir()
    run_git(path, "init")
    run_git(path, "config", "user.name", "Test User")
    run_git(path, "config", "user.email", "test@example.invalid")


def commit_file(repository: Path, path: str, content: str, *, date: str) -> str:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    run_git(repository, "add", "--", path)
    run_git(repository, "commit", "-m", f"update {path}", date=date)
    return run_git(repository, "rev-parse", "HEAD")


def test_last_change_uses_path_separator_and_returns_exact_iso_time(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    initialize_repository(repository)
    first_sha = commit_file(
        repository,
        "--all.txt",
        "first\n",
        date="2026-01-02T03:04:05+00:00",
    )
    second_sha = commit_file(
        repository,
        "--all.txt",
        "second\n",
        date="2026-02-03T04:05:06+00:00",
    )
    assert first_sha != second_sha
    commit_file(
        repository,
        "unrelated.txt",
        "unrelated\n",
        date="2026-03-04T05:06:07+00:00",
    )

    provenance = GitProvenance(repository).last_change("--all.txt")

    assert provenance is not None
    assert provenance.path == "--all.txt"
    assert provenance.sha == second_sha
    assert provenance.committed_at == datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    assert provenance.committed_at_iso == "2026-02-03T04:05:06Z"


def test_nested_repository_path_and_convenience_function(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    initialize_repository(repository)
    expected_sha = commit_file(
        repository,
        "docs/feed.yaml",
        "signals: []\n",
        date="2026-04-05T06:07:08+00:00",
    )

    provenance = get_file_provenance(repository / "docs", "feed.yaml")

    assert provenance is not None
    assert provenance.commit_sha == expected_sha
    assert provenance.path == "docs/feed.yaml"


def test_untracked_file_and_empty_repository_return_none(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    initialize_repository(empty)
    (empty / "untracked.yaml").write_text("signals: []\n", encoding="utf-8")

    reader = GitProvenance(empty)

    assert reader.last_change("untracked.yaml") is None

    commit_file(
        empty,
        "tracked.yaml",
        "signals: []\n",
        date="2026-05-06T07:08:09+00:00",
    )
    assert reader.last_change("still-untracked.yaml") is None


def test_paths_outside_repository_and_control_characters_are_rejected(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    initialize_repository(repository)
    commit_file(
        repository,
        "tracked.yaml",
        "signals: []\n",
        date="2026-06-07T08:09:10+00:00",
    )
    reader = GitProvenance(repository)

    with pytest.raises(InvalidGitPathError, match="inside the repository"):
        reader.last_change(tmp_path / "outside.yaml")
    with pytest.raises(InvalidGitPathError, match="control-free"):
        reader.last_change("bad\npath.yaml")
    with pytest.raises(InvalidGitPathError, match="not the repository"):
        reader.last_change(".")


def test_non_repository_has_clear_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "not-a-repository"
    directory.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(directory.parent))

    with pytest.raises(GitProvenanceError, match="locate repository root"):
        GitProvenance(directory)
