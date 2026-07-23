# Privacy and security boundaries

Sunset Sentinel API is local-first. Manual feeds, OpenAPI files, reports, and the default SQLite
database remain on the machine where the process runs.

Network scanning is disabled unless the caller supplies both a URL and a matching hostname
allowlist entry. Requests do not follow redirects outside the allowlist. URL user information is
rejected, sensitive query values are masked in diagnostics, responses are not stored in full, and
only lifecycle-relevant headers are retained in the bounded cache.

The application does not authenticate to third-party APIs, discover credentials, execute
migration instructions, or guarantee that a provider will honor a published retirement date.
Sunset and deprecation signals are treated as hints that require human verification.
