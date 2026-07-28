# Changelog

All notable changes are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-07-28

### Added

- RFC-aware parsing for `Sunset`, `Deprecation`, `Link`, `Retry-After`, and
  `Cache-Control` lifecycle signals.
- OpenAPI, manual declaration, consumer-map, and rate-limited HTTP scan inputs
  with authoritative snapshot reconciliation in SQLite.
- Deterministic priority, confidence, impact-radius, and contradiction scoring.
- CLI and FastAPI interfaces for scanning, assessment, history, health, and
  export workflows.
- Markdown, JSON, iCalendar, migration-checklist, and GitHub issue-draft
  exporters.
- A network-free bundled demo with synthetic fixtures, benchmark evidence, and
  an accessible static GitHub Pages dashboard.
- Reproducible Python packaging, Docker Compose support, isolated wheel
  verification, SHA-256 release manifests, and archive-hygiene checks.
- CI, dependency and secret scanning, Pages deployment, and tag-triggered
  GitHub Release workflows.
- English and Simplified Chinese documentation, architecture and privacy notes,
  contribution guidance, and release policy.

### Fixed

- Stale or withdrawn source observations can no longer overwrite newer
  lifecycle state.
- Cross-process origin pacing and persisted `Retry-After` windows prevent
  concurrent scanners from bypassing host rate limits.
- Oversized cache and retry delays are clamped before date arithmetic.
- Structured Field parameters, iCalendar folding, packaged sample discovery,
  and default demo behavior now fail safely and deterministically.

### Security

- HTTP scans remain opt-in, redact sensitive headers, and reject unsupported
  schemes.
- CI audits locked dependencies and scans tracked files for high-confidence
  secret patterns.

[Unreleased]: https://github.com/KanadeK/sunset-sentinel-api/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/KanadeK/sunset-sentinel-api/releases/tag/v0.1.0
