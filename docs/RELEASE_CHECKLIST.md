# Release checklist

This checklist is a gate, not a release log. Leave every item unchecked until the command has
completed successfully for the exact commit being released. Version examples below use
`v0.1.0`; substitute the intended version for later releases.

## 1. Identity and clean-tree preflight

- [ ] `gh auth status` succeeds and the authenticated account owns the intended repository.
- [ ] Configure the local commit identity from that same account:

  ```bash
  GH_LOGIN="$(gh api user --jq .login)"
  GH_ID="$(gh api user --jq .id)"
  GH_EMAIL="$(gh api user --jq '.email // empty')"
  if [ -z "$GH_EMAIL" ]; then
    GH_EMAIL="${GH_ID}+${GH_LOGIN}@users.noreply.github.com"
  fi
  git config user.name "$GH_LOGIN"
  git config user.email "$GH_EMAIL"
  git config --local --get user.name
  git config --local --get user.email
  ```

- [ ] `git status --short` prints nothing.
- [ ] `git branch --show-current` prints `main`.
- [ ] Author and committer are the authenticated user, with no co-author trailers:

  ```bash
  git log --format='%h %an <%ae> | %cn <%ce> | %s'
  git log --format='%B' | grep -i '^Co-authored-by:' && exit 1 || true
  ```

## 2. Version and release notes

- [ ] `src/sunset_sentinel_api/_version.py`, `pyproject.toml`, and the release heading in
  `CHANGELOG.md` all name the same version.
- [ ] The changelog describes the shipped behavior and has no unresolved release-note marker.
- [ ] Package metadata is inspectable:

  ```bash
  python -c "import sunset_sentinel_api as p; print(p.__version__)"
  python -m pip show sunset-sentinel-api
  ```

## 3. Reproducible local gates

- [ ] Use CPython 3.12 and install the pinned development environment plus the local package:

  ```bash
  python --version
  python -m pip install -r requirements-dev.lock
  python -m pip install --no-deps -e .
  ```

- [ ] The single quality entry point succeeds:

  ```bash
  python scripts/verify.py
  ```

  It must run, without skipped failures:

  ```bash
  python -m ruff format --check .
  python -m ruff check .
  python -m mypy src
  python -m pytest -q --cov=src --cov-report=term-missing --cov-fail-under=80
  python -m build --no-isolation
  ```

- [ ] The Make entry point reaches the same gate:

  ```bash
  make verify
  ```

- [ ] The test report includes unit, integration, and E2E paths; total source coverage is at
  least 80%.
- [ ] The privacy E2E proves that local query secrets, raw source digests, and signal keys do not
  appear in exports or command output.

## 4. Demo and benchmark evidence

- [ ] Regenerate every demo asset exclusively from bundled synthetic data:

  ```bash
  python scripts/demo.py
  python scripts/generate_demo_image.py
  make demo
  ```

- [ ] Review `docs/demo/assessment.json`, the Markdown report, ICS calendar, checklist, issue
  drafts, and terminal-style PNG. Confirm the reported record counts agree.
- [ ] Confirm the PNG is a render of the real CLI assessment, not a fabricated browser
  screenshot. See [Demo provenance](DEMO.md).
- [ ] Run the benchmark on the release machine and commit only its measured output:

  ```bash
  python scripts/benchmark.py
  make benchmark
  ```

- [ ] `docs/demo/benchmark.json` and the generated block in [Benchmark](BENCHMARK.md) agree on
  machine metadata, dataset counts, iterations, median, and p95.

## 5. Package and container gates

- [ ] Build the release bundle and checksums:

  ```bash
  python scripts/package_release.py
  make package
  ```

- [ ] `dist-release/` contains the wheel, source distribution, demo report/calendar assets, and
  `SHA256SUMS.txt`.
- [ ] The packaging script installs the wheel into a clean temporary environment and runs a real
  bundled-sample smoke assessment.
- [ ] Verify the distribution metadata and contents:

  ```bash
  python -m build --no-isolation
  python -m zipfile --list dist/sunset_sentinel_api-0.1.0-py3-none-any.whl
  python -m tarfile --list dist/sunset_sentinel_api-0.1.0.tar.gz
  ```

- [ ] Validate and build the container configuration:

  ```bash
  docker compose config
  docker build --tag sunset-sentinel-api:0.1.0 .
  ```

- [ ] The image runs as the non-root user, the Compose port is published only on
  `127.0.0.1:8000`, and `/api/health` reports a ready database.

## 6. Automated release check

- [ ] Run the release policy gate from a clean tree after packaging:

  ```bash
  python scripts/release_check.py
  make release-check
  ```

- [ ] It verifies at least: clean worktree, version consistency, changelog entry, required
  artifacts, full quality gate, secret scan, empty-implementation scan, and author/committer
  identity.
- [ ] Review the fallback searches directly:

  ```bash
  git grep -nE 'TODO|FIXME|NotImplemented|placeholder|coming soon|lorem ipsum' -- \
    ':!docs/ROADMAP.md' || true
  git status --short
  git log --format='%h %an <%ae> | %cn <%ce> %s'
  ```

## 7. Remote `main` gates

- [ ] Push the verified commit, then prove remote `main` points at it:

  ```bash
  git push origin main
  git fetch origin main
  test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
  ```

- [ ] Wait for the exact `main` commit's `ci.yml` run:

  ```bash
  HEAD_SHA="$(git rev-parse HEAD)"
  CI_RUN_ID="$(gh run list --workflow ci.yml --commit "$HEAD_SHA" --limit 1 \
    --json databaseId,headSha --jq '.[0].databaseId')"
  gh run watch "$CI_RUN_ID" --exit-status
  gh run view "$CI_RUN_ID" --json conclusion,headSha,url
  ```

- [ ] Wait for the exact commit's `security.yml` run in the same way:

  ```bash
  SECURITY_RUN_ID="$(gh run list --workflow security.yml --commit "$HEAD_SHA" --limit 1 \
    --json databaseId,headSha --jq '.[0].databaseId')"
  gh run watch "$SECURITY_RUN_ID" --exit-status
  gh run view "$SECURITY_RUN_ID" --json conclusion,headSha,url
  ```

- [ ] Both `headSha` values equal `git rev-parse HEAD`, both conclusions are `success`, and no
  required check is merely skipped or configured `continue-on-error`.

Do not create or push a release tag while either remote gate is missing, pending, cancelled, or
red.

## 8. Tag and release workflow

- [ ] Create the annotated tag only after all earlier gates pass:

  ```bash
  git tag -a v0.1.0 -m "Release v0.1.0"
  test "$(git rev-list -n 1 v0.1.0)" = "$(git rev-parse origin/main)"
  git push origin v0.1.0
  ```

- [ ] Wait for the tag-triggered `release.yml` quality/build/publish run:

  ```bash
  RELEASE_SHA="$(git rev-list -n 1 v0.1.0)"
  RELEASE_RUN_ID="$(gh run list --workflow release.yml --commit "$RELEASE_SHA" \
    --event push --limit 1 \
    --json databaseId --jq '.[0].databaseId')"
  gh run watch "$RELEASE_RUN_ID" --exit-status
  gh run view "$RELEASE_RUN_ID" --json conclusion,headSha,url
  ```

- [ ] The release workflow rebuilt from the tag; it did not reuse unverified local output.
- [ ] The published release is final, points to the annotated tag, and exposes the expected
  assets:

  ```bash
  gh release view v0.1.0 \
    --json tagName,isDraft,isPrerelease,url,assets,targetCommitish
  ```

- [ ] Download the remote assets into a new directory and verify `SHA256SUMS.txt` against the
  downloaded files.
- [ ] Install the downloaded wheel in a clean Python 3.12 environment and run
  `sunset-sentinel --version` plus an offline bundled-sample assessment.

## 9. Public repository verification

- [ ] `gh repo view --json nameWithOwner,isPrivate,url,defaultBranchRef` reports the intended
  public repository and `main`.
- [ ] Repository description, topics (`api`, `deprecation`, `sunset`, `monitoring`, `openapi`),
  license, README links, and security-reporting link are correct.
- [ ] `git shortlog -sne --all`, GitHub's contributor list, and the release commit show only the
  intended human author.
- [ ] Record in the handoff: repository URL, release URL, main SHA, tag SHA, CI/security/release
  run URLs, test count, coverage, asset list, and every remote SHA-256.
