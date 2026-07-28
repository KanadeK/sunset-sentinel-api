from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
SITE = PROJECT_ROOT / "site"
WORKFLOW_NAMES = ("ci.yml", "security.yml", "release.yml", "pages.yml")


class SiteMarkupParser(HTMLParser):
    """Collect the small set of markup facts needed for static accessibility checks."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.elements.append((tag, dict(attrs)))


def _workflow(name: str) -> tuple[str, dict[str, Any]]:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    return text, parsed


def test_workflows_use_versioned_official_actions_and_fail_closed() -> None:
    for name in WORKFLOW_NAMES:
        text, _ = _workflow(name)
        assert "continue-on-error" not in text.lower()
        actions = re.findall(r"^\s*uses:\s*([^\s#]+)", text, flags=re.MULTILINE)
        assert actions
        assert all(action.startswith("actions/") for action in actions)
        assert all(re.search(r"@v\d+$", action) for action in actions)


def test_ci_runs_the_locked_full_quality_gate_and_uploads_evidence() -> None:
    text, workflow = _workflow("ci.yml")

    assert workflow["permissions"] == {"contents": "read"}
    for required in (
        "requirements-dev.lock",
        "--no-deps --editable .",
        "ruff format --check .",
        "ruff check .",
        "mypy src",
        "--cov-fail-under=80",
        "--cov-report=xml:coverage.xml",
        "--junitxml=test-results.xml",
        "python -m build --no-isolation",
        "scripts/demo.py",
        "scripts/generate_demo_image.py",
        "git diff --exit-code --",
        "coverage.xml",
        "test-results.xml",
        "dist/*",
        "sunset-sentinel-dashboard.png",
    ):
        assert required in text


def test_security_gate_audits_lock_and_scans_tracked_files() -> None:
    text, workflow = _workflow("security.yml")

    assert workflow["permissions"] == {"contents": "read"}
    assert "python -m pip_audit" in text
    assert "--requirement requirements-dev.lock" in text
    assert "--strict" in text
    assert "--no-deps" in text
    assert "--disable-pip" in text
    assert "git grep -I -n -E" in text
    assert "PRIVATE KEY" in text
    assert "high-confidence secret" in text


def test_release_gate_publishes_verified_assets_after_packaging() -> None:
    text, workflow = _workflow("release.yml")

    assert workflow["permissions"] == {"contents": "write"}
    assert '"v*"' in text
    assert "--cov-fail-under=80" in text
    assert "python scripts/package_release.py" in text
    assert "dist-release/**" in text
    assert "gh release create" in text
    assert "--draft" in text
    assert "gh release upload" in text
    assert "gh release edit" in text
    assert "--draft=false" in text
    assert "softprops/" not in text.lower()


def test_pages_deploys_only_whitelisted_synthetic_artifacts() -> None:
    text, workflow = _workflow("pages.yml")

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["deploy"]["permissions"] == {
        "pages": "write",
        "id-token": "write",
    }
    assert "cancel-in-progress: true" in text
    assert "needs: verify" in text
    assert "python scripts/verify.py" in text
    assert "git diff --exit-code --" in text
    assert "site/. _site/" in text
    assert "docs/demo/${artifact}" in text
    assert "actions/upload-pages-artifact@v3" in text
    assert "actions/deploy-pages@v4" in text
    assert "*.db" not in text


def test_site_has_accessible_structure_and_local_runtime_assets() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    parser = SiteMarkupParser()
    parser.feed(html)

    elements = parser.elements
    assert any(tag == "meta" and attrs.get("name") == "viewport" for tag, attrs in elements)
    assert any(
        tag == "a"
        and attrs.get("href") == "#main-content"
        and "skip-link" in (attrs.get("class") or "")
        for tag, attrs in elements
    )
    assert any(tag == "main" and attrs.get("id") == "main-content" for tag, attrs in elements)
    assert any(attrs.get("aria-live") == "polite" for _, attrs in elements)
    assert any(tag == "nav" and attrs.get("aria-label") for tag, attrs in elements)

    images = [attrs for tag, attrs in elements if tag == "img"]
    assert images
    assert all(attrs.get("alt") for attrs in images)

    resource_attributes = (
        (tag, attrs.get("src") or attrs.get("href"))
        for tag, attrs in elements
        if tag in {"img", "script", "link"}
    )
    assert all(
        value and not value.startswith(("http://", "https://", "//"))
        for _, value in resource_attributes
    )
    assert "onclick=" not in html.lower()


def test_site_links_every_real_demo_artifact_and_loads_assessment() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    javascript = (SITE / "app.js").read_text(encoding="utf-8")

    for artifact in (
        "assessment.json",
        "benchmark.json",
        "issue-drafts.json",
        "lifecycle.ics",
        "migration-checklist.md",
        "report.md",
        "sunset-sentinel-dashboard.png",
    ):
        assert f"demo/{artifact}" in html
    assert 'fetch("demo/assessment.json"' in javascript
    assert "synthetic fixture data only" in html.lower()


def test_site_visible_sources_avoid_em_and_en_dashes() -> None:
    for path in (SITE / "index.html", SITE / "app.js"):
        text = path.read_text(encoding="utf-8")
        assert "\N{EM DASH}" not in text
        assert "\N{EN DASH}" not in text


def test_site_styles_are_self_contained_and_motion_safe() -> None:
    css = (SITE / "styles.css").read_text(encoding="utf-8")

    assert "@import" not in css
    assert "gradient(" not in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ":focus-visible" in css
