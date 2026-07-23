# Release checklist

- [ ] Version matches package metadata, source constant, and changelog.
- [ ] Ruff formatting and lint checks pass.
- [ ] Mypy strict mode passes.
- [ ] Unit, integration, E2E, security, and regression tests pass at 80% or greater coverage.
- [ ] Wheel and source distribution build successfully.
- [ ] The wheel installs in a clean temporary environment and completes a real sample scan.
- [ ] The offline demo regenerates report, calendar, issue draft, checklist, and screenshot.
- [ ] Release artifacts have recorded SHA-256 checksums.
- [ ] Secret and empty-implementation scans pass.
- [ ] Git history contains only intended authors and no automated co-author trailers.
- [ ] Main CI and security workflows are green before the release tag is created.
