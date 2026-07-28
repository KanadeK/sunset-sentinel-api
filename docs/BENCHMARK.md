# Benchmark

The benchmark measures the complete local data path at the bundled sample scale. It is evidence
for a particular machine and commit, not a universal latency claim.

## Reproduce

From an installed Python 3.12 development environment:

```bash
python scripts/benchmark.py
```

The command uses only [`examples/openapi.yaml`](../examples/openapi.yaml),
[`examples/manual-feed.yaml`](../examples/manual-feed.yaml), and
[`examples/consumers.json`](../examples/consumers.json). It does not start the fixture server,
sleep, or contact an external API.

One measured iteration:

1. creates a new temporary SQLite database;
2. imports all three bundled sources;
3. assesses the persisted lifecycle records at a fixed UTC time; and
4. renders JSON, Markdown, ICS, migration-checklist, and issue-draft outputs.

The script records all iterations, then reports the measured median and p95 wall-clock duration
for that complete operation. It atomically writes the machine-readable result to
`docs/demo/benchmark.json` and prints the same summary. A maintainer then copies those exact
values into the generated block below; the script does not invent or embed a baseline.

## Machine-readable format

`docs/demo/benchmark.json` has this shape. Angle-bracket values describe measured output; they
are not sample results.

```text
{
  "schema_version": 1,
  "generated_at": "<execution timestamp in RFC3339 UTC>",
  "scenario_at": "2026-07-23T00:00:00Z",
  "machine": {
    "platform": "<platform description>",
    "python": "<Python version>",
    "processor": "<reported processor or unknown>"
  },
  "data": {
    "consumers": "<integer>",
    "dependencies": "<integer>",
    "signals": "<integer>",
    "records": "<integer>",
    "rendered_formats": "<integer>"
  },
  "iterations": "<integer>",
  "median_ms": "<measured number>",
  "p95_ms": "<measured number>"
}
```

Dataset counts are emitted from the real import and assessment rather than hard-coded into this
document. Timing values must come from `scripts/benchmark.py`; do not hand-edit or estimate them.

## Recorded result

<!-- benchmark-results:start -->
Measured on 2026-07-28 with CPython 3.12.13 on Windows 11
(`Windows-11-10.0.26200-SP0`, 32 logical CPUs):

- Workload: fresh SQLite import, assessment, and five-format render
- Dataset: 2 consumers, 3 dependencies, 3 signals, and 3 lifecycle records
- Iterations: 3 warmups followed by 25 measured runs
- Median: **57.106 ms**
- p95: **61.504 ms**

The full samples and machine metadata are committed in
[`docs/demo/benchmark.json`](demo/benchmark.json).
<!-- benchmark-results:end -->

## Interpretation limits

- Results compare runs on the same machine and power/runtime configuration most reliably.
- Antivirus, filesystem, virtualization, CPU scaling, and other local load can affect SQLite and
  render timing.
- This small synthetic dataset demonstrates interactive local behavior; it is not a capacity
  limit or production load test.
- The benchmark intentionally excludes network latency and browser rendering.
- Retain the generated JSON with release evidence so reported numbers can be traced to their
  machine metadata and dataset counts.
