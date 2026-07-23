# Architecture

Sunset Sentinel API separates deterministic lifecycle analysis from external I/O.

```text
CLI / FastAPI
      |
application services
      |
domain models, parsers, assessment, exporters
      |
file / HTTP / Git / SQLite adapters
```

- The domain layer receives timestamps through an injected clock and performs no network access.
- Adapters normalize HTTP headers, OpenAPI documents, and manual feeds into the same signal model.
- Services preserve first discovery, calculate current priority, and coordinate exports.
- SQLite stores observations, change history, and bounded HTTP cache entries.
- Network access is opt-in and constrained by an explicit hostname allowlist.

Detailed data flow and schemas are documented alongside the completed domain and adapter
milestones.
