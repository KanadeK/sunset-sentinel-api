# Architecture

Sunset Sentinel API is a local-first application. Its deterministic lifecycle model is isolated
from files, HTTP, SQLite, scheduling, and presentation so the core can be exercised without a
network or UI.

## Runtime map

```text
OpenAPI / manual feed / consumer map       explicitly allowlisted HTTPS endpoint
                  |                                      |
          strict file adapters                     HTTP policy gate
                  |                                      |
                  +---------- normalized signals --------+
                                      |
                           monitor / HTTP scan services
                                      |
                           SQLite repository + cache
                                      |
                      reconciliation and risk scoring
                                      |
             JSON / Markdown / ICS / checklist / issue drafts
                                      |
                         CLI or loopback FastAPI UI
```

The dependency direction points inward: interfaces call services, services call domain logic
through adapters, and the domain does not import FastAPI, APScheduler, HTTPX, or SQLite.

## Components

### Domain core

[`domain/models.py`](../src/sunset_sentinel_api/domain/models.py) defines immutable, strict
Pydantic values for lifecycle signals, endpoints, consumers, dependencies, records, and
diagnostics. Dates are normalized to second-precision UTC and naive timestamps are rejected.

[`domain/headers.py`](../src/sunset_sentinel_api/domain/headers.py) parses RFC and compatibility
forms of `Deprecation`, `Sunset`, and lifecycle `Link` headers.
[`domain/lifecycle.py`](../src/sunset_sentinel_api/domain/lifecycle.py) selects the lifecycle
state, while [`domain/scoring.py`](../src/sunset_sentinel_api/domain/scoring.py) calculates
urgency, blast radius, and priority from the injected assessment time and affected consumers.

### Adapters

- [`adapters/file_sources.py`](../src/sunset_sentinel_api/adapters/file_sources.py) accepts
  OpenAPI 3.0/3.1, schema-versioned manual feeds, and consumer maps. It limits each source to
  5 MiB, decodes only UTF-8, uses a safe YAML loader, rejects duplicate keys, validates all
  documents before ingestion, and produces one sorted `SourceBatch`.
- [`adapters/http_client.py`](../src/sunset_sentinel_api/adapters/http_client.py) is the only
  outbound HTTP path. It applies URL and host policy before transport, disables redirect
  following, rate-limits per origin, supports conditional requests, and returns retained
  lifecycle metadata without reading a response body.
- [`adapters/sqlite_repository.py`](../src/sunset_sentinel_api/adapters/sqlite_repository.py)
  provides transactional persistence. It enables WAL, foreign keys, a busy timeout, strict
  tables, and rollback on failed writes.
- [`adapters/sqlite_http_cache.py`](../src/sunset_sentinel_api/adapters/sqlite_http_cache.py)
  connects the HTTP cache and request-pacing ports to SQLite. The cache is bounded by entry count
  and serialized metadata size; origin pacing uses a hashed origin key.
- [`adapters/fixture_server.py`](../src/sunset_sentinel_api/adapters/fixture_server.py) supplies
  deterministic loopback HTTP scenarios for tests and demonstrations.
- [`adapters/git_provenance.py`](../src/sunset_sentinel_api/adapters/git_provenance.py) is a
  read-only helper for tracked-file provenance. It is not part of the default import command.

### Application services

[`services/monitor.py`](../src/sunset_sentinel_api/services/monitor.py) loads and persists local
source batches, reconciles authoritative snapshots, and reconstructs an assessment from durable
first/last-seen intervals.
[`services/http_scan.py`](../src/sunset_sentinel_api/services/http_scan.py) turns a safe HTTP
fetch into header-derived evidence and withdraws a prior signal only when both lifecycle headers
are now absent.

[`services/assessment.py`](../src/sunset_sentinel_api/services/assessment.py) groups signals by
service or endpoint, uses the earliest active dates, marks conflicting evidence, joins matching
consumer dependencies, and invokes scoring. The operation is pure for a given input and clock.

[`services/scheduler.py`](../src/sunset_sentinel_api/services/scheduler.py) wraps APScheduler.
Jobs use a stable hash-derived ID, `max_instances=1`, coalescing, UTC triggers, and a validated
interval from one minute through one year.

### Interfaces and exporters

[`cli.py`](../src/sunset_sentinel_api/cli.py) exposes database initialization, local import,
allowlisted HTTP scan, reporting, demo generation, local-source watch jobs, and the web server.
The `watch --once` path invokes the registered task synchronously. Long-running watch mode starts
the background scheduler, refreshes local inputs on the interval, and shuts down on
`KeyboardInterrupt`. Each refresh writes all five exports through same-directory temporary files,
`fsync`, and `os.replace`.

[`api.py`](../src/sunset_sentinel_api/api.py) serves read-only health, records, changes, and export
routes plus one explicit sample-import mutation. The supported CLI server binds only to loopback.

[`exporters.py`](../src/sunset_sentinel_api/exporters.py) owns the safe public projection:

- deterministic JSON assessment;
- human-readable Markdown;
- RFC 5545 calendar events;
- stable migration checklists; and
- local GitHub-compatible issue drafts with no remote side effect.

## Data flows

### Local import

1. The CLI parses `--openapi TARGET=PATH`, `--feed`, and `--consumers`.
2. Every file is read and validated before persistence starts. A malformed or missing file
   therefore fails before the batch is ingested.
3. The monitor service writes consumers, dependency edges, and signals in foreign-key-safe order.
   Each upsert is its own immediate SQLite transaction.
4. Signal upserts preserve first/last-seen time and append only material `discovered`, `updated`,
   or `withdrawn` changes.
5. Supplied OpenAPI targets, manual feeds, and consumer maps are authoritative for their
   respective snapshot scopes. Missing signals are marked inactive; missing consumer edges and
   consumers are removed. Omitted source categories are not reconciled.

### HTTP scan

1. Scheme, credentials, fragment, hostname, loopback status, and the explicit allowlist are
   checked before a request object is sent.
2. A fresh cache entry can satisfy the scan. A stale entry supplies `ETag` or `Last-Modified`
   validators. An immediate SQLite transaction atomically claims the per-origin request slot,
   and 429/503 `Retry-After` deadlines survive process recreation.
3. Only selected headers are retained. `Link` targets and the display URL are redacted before
   cache persistence.
4. Parsed evidence is normalized into the same `LifecycleSignal` model used by file sources and
   then upserted into SQLite. If a successful refreshed response contains neither lifecycle
   header, the matching prior HTTP signal is marked inactive.

See [Privacy and security boundaries](PRIVACY_AND_SECURITY.md) for the exact retained fields and
redaction limits.

### Assessment and export

The repository supplies current signals and observation intervals. Assessment joins consumer
dependencies, reconciles dates, and scores records at the caller-supplied time. Exporters then
build a reduced view that deliberately excludes raw signal identities, source references, raw
digests, and diagnostics. The CLI can print that view, write it to a file, or serve it through the
loopback API.

## SQLite model

| Table | Purpose |
| --- | --- |
| `consumers` | Current consumer metadata and first/last-seen timestamps |
| `consumer_dependencies` | Consumer-to-endpoint edges with observation timestamps |
| `lifecycle_signals` | Current normalized signal payload and observation interval |
| `changes` | Material previous/current signal snapshots |
| `http_cache` | Bounded status, retained headers, validators, times, and redacted URL |
| `origin_request_state` | Atomic last-request and `Retry-After` state keyed by origin digest |

The database is the private source of truth, not a publishable report. It is not encrypted by the
application; filesystem access control and backup policy belong to the operator.

## Determinism and failure behavior

- Domain and service code receive a clock; tests and repeatable commands use fixed UTC times.
- File adapters and assessment results are sorted before persistence or rendering.
- No test or bundled demo needs an external API.
- Parsing and policy failures are surfaced as non-zero CLI results rather than converted to
  successful empty data.
- Public exports are deterministic for the same database and assessment time.

## Deployment boundary

Version 0.1.0 is a single-user local tool, not an authenticated multi-tenant service.
`sunset-sentinel serve` accepts only `127.0.0.1`, `::1`, or `localhost`. The container listens
inside its network namespace, while the supported Compose configuration publishes the port on
host loopback only. Exposing the ASGI application through another proxy or interface requires an
independent authentication, authorization, TLS, and CSRF design.
