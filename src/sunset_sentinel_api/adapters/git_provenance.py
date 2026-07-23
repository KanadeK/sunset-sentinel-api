"""Read-only Git provenance for files referenced by local lifecycle evidence."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sunset_sentinel_api.domain.models import as_utc

_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FIELD_SEPARATOR = "\x1f"


class GitProvenanceError(RuntimeError):
    """A local repository could not provide trustworthy provenance."""


class InvalidGitPathError(GitProvenanceError, ValueError):
    """A requested file path was unsafe or outside the repository."""


@dataclass(frozen=True, slots=True)
class GitFileProvenance:
    """The most recent commit that changed one repository-relative file."""

    path: str
    commit_sha: str
    committed_at: datetime

    @property
    def sha(self) -> str:
        """Alias useful to callers that use the shorter Git term."""

        return self.commit_sha

    @property
    def committed_at_iso(self) -> str:
        """Return the commit instant as a UTC ISO 8601 string."""

        return self.committed_at.isoformat().replace("+00:00", "Z")


class GitProvenance:
    """Query one local Git repository without modifying its worktree or index."""

    def __init__(
        self,
        repository_path: str | Path,
        *,
        git_executable: str | Path | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        requested_path = Path(repository_path)
        if not requested_path.is_dir():
            raise GitProvenanceError(f"Git repository path is not a directory: {requested_path}")

        executable = str(git_executable) if git_executable is not None else shutil.which("git")
        if executable is None:
            raise GitProvenanceError("git executable was not found")

        self._git_executable = executable
        self._timeout_seconds = timeout_seconds
        initial_root = requested_path.resolve()
        self._base_path = initial_root
        result = self._run(
            initial_root,
            ("rev-parse", "--show-toplevel"),
            operation="locate repository root",
        )
        root_text = result.stdout.strip()
        if not root_text:
            raise GitProvenanceError("git returned an empty repository root")
        root = Path(root_text).resolve()
        if not root.is_dir():
            raise GitProvenanceError("git returned a repository root that is not a directory")
        self._repository_root = root

    @property
    def repository_root(self) -> Path:
        """Return Git's canonical top-level directory."""

        return self._repository_root

    def last_change(self, file_path: str | Path) -> GitFileProvenance | None:
        """Return the last commit for a safe file path, or ``None`` if untracked."""

        relative_path = self._relative_path(file_path)
        head = self._run(
            self._repository_root,
            ("rev-parse", "--verify", "HEAD"),
            operation="resolve HEAD",
            check=False,
        )
        if head.returncode != 0:
            return None

        result = self._run(
            self._repository_root,
            (
                "--no-pager",
                "log",
                "-1",
                "--follow",
                f"--format=%H{_FIELD_SEPARATOR}%cI",
                "--",
                relative_path,
            ),
            operation=f"read provenance for {relative_path}",
        )
        output = result.stdout.strip()
        if not output:
            return None
        fields = output.split(_FIELD_SEPARATOR)
        if len(fields) != 2:
            raise GitProvenanceError("git log returned an unexpected provenance record")
        commit_sha, raw_timestamp = fields
        if not _COMMIT_RE.fullmatch(commit_sha):
            raise GitProvenanceError("git log returned an invalid commit identifier")
        try:
            committed_at = as_utc(
                datetime.fromisoformat(raw_timestamp),
                field_name="git commit timestamp",
            )
        except ValueError as exc:
            raise GitProvenanceError("git log returned an invalid ISO commit timestamp") from exc
        return GitFileProvenance(
            path=relative_path,
            commit_sha=commit_sha,
            committed_at=committed_at,
        )

    def _relative_path(self, file_path: str | Path) -> str:
        raw_path = os.fspath(file_path)
        if not raw_path or _CONTROL_RE.search(raw_path):
            raise InvalidGitPathError("Git file path must be non-empty and control-free")
        candidate = Path(file_path)
        if not candidate.is_absolute():
            candidate = self._base_path / candidate
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(self._repository_root)
        except ValueError as exc:
            raise InvalidGitPathError("Git file path must stay inside the repository") from exc
        if not relative.parts:
            raise InvalidGitPathError("Git file path must identify a file, not the repository")
        return relative.as_posix()

    def _run(
        self,
        repository: Path,
        arguments: tuple[str, ...],
        *,
        operation: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            self._git_executable,
            "-C",
            str(repository),
            *arguments,
        ]
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        try:
            result = subprocess.run(  # noqa: S603
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise GitProvenanceError("git executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitProvenanceError(f"git timed out while attempting to {operation}") from exc
        if check and result.returncode != 0:
            detail = result.stderr.strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise GitProvenanceError(f"git could not {operation}{suffix}")
        return result


def get_file_provenance(
    repository_path: str | Path,
    file_path: str | Path,
) -> GitFileProvenance | None:
    """Convenience wrapper for a single read-only provenance query."""

    return GitProvenance(repository_path).last_change(file_path)
