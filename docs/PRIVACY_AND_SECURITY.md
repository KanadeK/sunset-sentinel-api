# Privacy and security boundaries

Sunset Sentinel API is local-first, but “local” does not mean “non-sensitive.” The SQLite
database is a private working store. JSON, Markdown, calendar, checklist, and issue-draft exports
use a smaller, redacted projection intended for review or sharing.

## Trust boundaries

| Boundary | Untrusted input | Control |
| --- | --- | --- |
| Local files | OpenAPI, YAML/JSON manual feeds, consumer maps | 5 MiB limit, UTF-8 only, safe YAML, duplicate-key rejection, strict schemas |
| HTTP | URL and remote response metadata | explicit hostname allowlist, HTTPS by default, URL policy before transport, no redirects |
| Private state | SQLite database and WAL files | local path chosen by operator; transactional schema; no application-level encryption |
| Public projection | CLI/API exports and dashboard records | query-value masking and field allowlists |
| Browser | requests to the local FastAPI app | loopback-only CLI binding, no-store and browser security headers, explicit mutation header |

The tool does not discover credentials, authenticate to provider APIs, send local source files to
a hosted service, or publish issue drafts.

## What local file import stores

OpenAPI and manual-feed lifecycle signals are persisted as complete normalized
`LifecycleSignal` JSON. The following values enter the local database without export masking:

- stable `signal_key`, `target_id`, source type, and a source reference containing the source
  filename and document pointer;
- endpoint target, method, path, and optional operation ID;
- deprecation flag, deprecation/sunset times, active state, compliance mode, and observation
  time;
- `documentation_url` and `replacement`, including original query values;
- the SHA-256 digest of the complete source file; and
- any normalized diagnostics attached to the signal.

Consumer imports likewise retain consumer IDs and names, criticality, owner, repository path,
tags, endpoint keys, and dependency evidence exactly as supplied after validation. Material
change rows contain previous/current signal JSON, so sensitive signal fields can also exist in
history after the current value changes.

The raw OpenAPI/feed bytes are not copied into SQLite, but their SHA-256 digest is retained.
Unrelated OpenAPI content that does not become lifecycle evidence is not persisted.

**Operational consequence:** do not put credentials in lifecycle URLs, replacement text,
consumer ownership fields, paths, or evidence. If a local source contains a secret, remove it
from the source and rotate it; deleting an export is not enough because the database, WAL,
backups, and change history may still contain it.

## HTTP collection and cache

An HTTP scan requires at least one `--allow-host`. Exact entries match only that host. A wildcard
such as `*.example.com` matches subdomains but not the apex. URL policy runs before transport:

- only HTTP(S) URLs are accepted;
- username/password URL information and fragments are rejected;
- the normalized hostname must match the explicit allowlist;
- HTTPS is required, except for an explicitly enabled loopback fixture; and
- loopback is denied unless `--allow-loopback` is supplied.

Redirect following is disabled. A redirect response is reported but its destination is not
requested. Per-origin request spacing, bounded timeouts, cache freshness, conditional requests,
and `Retry-After` handling reduce unnecessary traffic. Request slots and retry deadlines are
coordinated atomically in SQLite, so one-shot CLI processes share the same pacing boundary. The
state table stores a SHA-256 origin key and timestamps, not the request URL.

The response is opened as a stream and its body is never read or cached. Only these response
headers may be retained:

```text
Sunset
Deprecation
Link
ETag
Last-Modified
Cache-Control
Date
Retry-After
```

`Cookie`, `Set-Cookie`, `Authorization`, `Location`, and all other fields are excluded.
Lifecycle `Link` targets have every query value replaced with `REDACTED` before the header can
enter either the cache or a persisted HTTP-derived signal. The requested URL is stored only as a
redacted display URL plus a SHA-256 cache key derived from the full normalized request URL.
Response bodies are never part of that key or cache.

The default SQLite cache permits at most 256 entries, at most 65,536 serialized metadata bytes
per entry, and at most a six-hour HTTP freshness lifetime. These are resource bounds, not a
confidentiality mechanism.

## What public exports reveal

All five exporters share the same safe projection:

- target ID, service/endpoint scope, method, path, and operation ID;
- lifecycle state and effective dates;
- first/last-seen times;
- urgency, blast-radius, and priority scores;
- consumer ID, name, and criticality;
- signal source type, compliance, and active state;
- documentation URLs and replacements after output sanitization.

They exclude raw `signal_key`, `source_ref`, signal/file `raw_sha256`, diagnostics, consumer
owner, repository path, dependency evidence, and previous/current change snapshots. The changes
API exposes a short hash-derived signal ID instead of the raw signal key.

For every HTTP(S) documentation URL or replacement URL, each query component keeps its key but
replaces its value:

```text
https://docs.example.test/migrate?token=secret&account=42
https://docs.example.test/migrate?token=REDACTED&account=REDACTED
```

This rule is applied separately by JSON, Markdown, ICS, checklist/issue generation paths wherever
the URL is rendered. HTTP user information is not emitted. Non-URL replacement text is treated as
display text: line breaks are flattened, but its words are not secret-scanned or redacted.

Exports are therefore **redacted, not anonymous**. Hosts, URL paths, query parameter names,
fragments, target IDs, endpoint paths, consumer names, and free-form replacement text can still
be sensitive. Review artifacts before posting them publicly.

## Loopback Web boundary

`sunset-sentinel serve` rejects non-loopback bind hosts. The supported Docker Compose mapping is
also host-loopback only. The application intentionally has no user accounts or remote access
control.

Read routes expose health counts, records, changes, and downloadable exports to any process that
can reach the listener. The only mutation, `POST /api/import/sample`, also requires:

```text
X-Sunset-Sentinel: dashboard-v1
```

This fixed header is an explicit local mutation confirmation, not authentication and not a
secret. It does not make internet exposure safe. The browser receives a restrictive
Content-Security-Policy, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, and `Cache-Control: no-store` for the dashboard/API.

Do not bind or proxy this v0.1.0 app to an untrusted network. A remote deployment needs a separate
security design for authentication, authorization, TLS, CSRF, rate limiting, and audit logging.

## Operator checklist

1. Keep the database, its `-wal`/`-shm` files, and backups private.
2. Use source files that contain no credentials or personal data.
3. Allowlist the narrowest exact provider host; use wildcards only when necessary.
4. Keep loopback exceptions limited to the bundled deterministic fixture.
5. Inspect every export before sharing it.
6. Rotate any secret that entered a source file or database; redaction does not undo exposure.

Report vulnerabilities through the private process in [`SECURITY.md`](../SECURITY.md), not a
public issue.
