# Demo provenance and reproduction

The repository demo is an offline, deterministic product walkthrough. Every lifecycle fact,
consumer, dependency, report, and image originates from files shipped in this repository.

## Source data

The only inputs are:

- [`examples/openapi.yaml`](../examples/openapi.yaml), a synthetic OpenAPI 3.1 service;
- [`examples/manual-feed.yaml`](../examples/manual-feed.yaml), a synthetic service retirement;
  and
- [`examples/consumers.json`](../examples/consumers.json), synthetic consumers and call-site
  evidence.

These fixtures are original MIT-licensed project data. They contain no production URL,
credential, personal data, copied provider response, or live third-party API result. The demo
does not start the HTTP fixture and does not require internet access.

## Regenerate

Install the project with Python 3.12, then run:

```bash
python scripts/demo.py
python scripts/generate_demo_image.py
```

The equivalent task entry point is:

```bash
make demo
```

The scripts use a fixed assessment time and a temporary local SQLite database. They invoke the
real import, persistence, assessment, scoring, and exporter paths. A failure propagates as a
non-zero result; the scripts do not substitute static success output.

The generated review set under `docs/demo/` is:

```text
assessment.json
report.md
lifecycle.ics
migration-checklist.md
issue-drafts.json
sunset-sentinel-dashboard.png
```

`assessment.json` is the canonical data behind the other views. Regeneration should yield three
assessed records from three normalized lifecycle signals, while calendar event count follows the
dates actually present in those records.

## What the PNG is

Browser screenshot capture is restricted by policy on the machine used for this release. The PNG
is therefore a reproducible **terminal-style rendering of the actual CLI assessment**, generated
by `scripts/generate_demo_image.py` from the newly produced assessment data.

It is not a browser screenshot, does not claim to show a live browser session, and does not invent
controls or records that are absent from the CLI output. The terminal treatment is presentation
only; displayed targets, states, dates, consumers, and scores come from the same real assessment
serialized to `assessment.json`.

## Verify provenance

After regeneration:

1. Confirm all six files above have a modification time from the current run.
2. Parse `assessment.json` and confirm it contains non-empty `records`.
3. Compare the record identities and scores with `report.md`.
4. Confirm the ICS begins with `BEGIN:VCALENDAR` and its events correspond to dates in the
   assessment.
5. Confirm each issue draft and checklist section names a real assessed record.
6. Confirm the PNG labels itself as a terminal/CLI assessment rather than a browser capture.
7. Run the privacy E2E before publishing any artifact.

For the data-retention and export-redaction boundary, see
[Privacy and security boundaries](PRIVACY_AND_SECURITY.md). For performance evidence, see
[Benchmark](BENCHMARK.md).
