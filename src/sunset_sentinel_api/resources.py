"""Locate read-only resources in source and wheel installations."""

from __future__ import annotations

from pathlib import Path


def bundled_sample_directory() -> Path:
    """Return the synthetic sample directory for this installation."""

    package_samples = Path(__file__).resolve().parent / "examples"
    if package_samples.is_dir():
        return package_samples

    source_samples = Path(__file__).resolve().parents[2] / "examples"
    if source_samples.is_dir():
        return source_samples
    return package_samples
