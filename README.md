# Sunset Sentinel API

Sunset Sentinel API turns API lifecycle signals into an actionable migration queue. It reads
standard HTTP `Sunset` and `Deprecation` headers, OpenAPI deprecation metadata, and local feeds
without sending sample data to an external service.

The repository is being built as a local-first Python 3.12 application with a deterministic
domain core, SQLite persistence, a CLI, and a FastAPI interface. The current source tree exposes
the package metadata and strict quality gate; the complete v0.1.0 usage guide is delivered with
the release milestone.

A sample search of public repositories found no active project with both the same name and a
highly isomorphic feature set. See [the documented sample and differentiation](docs/COMPETITOR_SCAN.md).

```bash
python -m pip install -e ".[dev]"
python scripts/verify.py
sunset-sentinel --version
```

License: [MIT](LICENSE). Security reports: [SECURITY.md](SECURITY.md).
