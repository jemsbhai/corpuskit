"""Clean-source contract for every documented CorpusKit web route."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WEB_APP_ROOT = REPOSITORY_ROOT / "apps" / "web" / "src" / "app"
SITE_HEADER = REPOSITORY_ROOT / "apps" / "web" / "src" / "components" / "site-header.tsx"

PUBLIC_PAGE_ROUTES = (
    "/",
    "/advanced",
    "/analysis",
    "/artifacts",
    "/capabilities",
    "/coverage",
    "/evaluate",
    "/g2p",
    "/generation",
    "/inventory",
    "/jobs",
    "/projects",
    "/selection",
)


def test_every_public_page_route_is_present_in_the_source_tree() -> None:
    missing = [
        route
        for route in PUBLIC_PAGE_ROUTES
        if not (WEB_APP_ROOT / route.removeprefix("/") / "page.tsx").is_file() and route != "/"
    ]
    if not (WEB_APP_ROOT / "page.tsx").is_file():
        missing.insert(0, "/")

    assert missing == []


def test_public_page_sources_are_not_excluded_by_repository_ignore_rules() -> None:
    page_paths = tuple(
        str((WEB_APP_ROOT / route.removeprefix("/") / "page.tsx").relative_to(REPOSITORY_ROOT))
        for route in PUBLIC_PAGE_ROUTES
        if route != "/"
    )
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(  # noqa: S603 - fixed executable and repository-owned paths
        (git, "check-ignore", "--no-index", "--", *page_paths),
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1, result.stdout


def test_primary_navigation_has_a_source_page_for_every_link() -> None:
    source = SITE_HEADER.read_text(encoding="utf-8")
    navigation = tuple(re.findall(r'\{ href: "(/[^"]*)", label:', source))

    assert len(navigation) == len(set(navigation))
    assert set(navigation) == set(PUBLIC_PAGE_ROUTES)
