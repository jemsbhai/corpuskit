"""Repository documentation integrity contracts."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).parents[2]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)]+)\)")


def _markdown_files() -> tuple[Path, ...]:
    documents = set(ROOT.glob("*.md"))
    for source_root in (ROOT / "apps", ROOT / "deploy", ROOT / "docs"):
        documents.update(
            path
            for path in source_root.rglob("*.md")
            if "node_modules" not in path.parts and ".next" not in path.parts
        )
    return tuple(sorted(documents))


@pytest.mark.parametrize(
    "document", _markdown_files(), ids=lambda path: str(path.relative_to(ROOT))
)
def test_relative_markdown_links_resolve_inside_repository(document: Path) -> None:
    """Keep every checked-in relative documentation and image link resolvable."""

    failures: list[str] = []
    for match in MARKDOWN_LINK.finditer(document.read_text(encoding="utf-8")):
        raw_target = match.group("target").strip()
        if raw_target.startswith("<") and raw_target.endswith(">"):
            raw_target = raw_target[1:-1]
        target = urlsplit(raw_target)
        if target.scheme or target.netloc or raw_target.startswith("#"):
            continue
        relative_path = unquote(target.path)
        if not relative_path:
            continue
        resolved = (
            (ROOT / relative_path.lstrip("/"))
            if relative_path.startswith("/")
            else (document.parent / relative_path)
        )
        if not resolved.resolve().is_relative_to(ROOT.resolve()):
            failures.append(f"link escapes repository: {raw_target}")
        elif not resolved.exists():
            failures.append(f"missing target: {raw_target}")

    assert not failures, f"{document.relative_to(ROOT)}: " + "; ".join(failures)


def test_public_execution_routing_claims_match_the_registered_boundaries() -> None:
    overview = (ROOT / "docs" / "architecture" / "overview.md").read_text(encoding="utf-8")
    runtimes = (ROOT / "docs" / "operations" / "model-runtimes.md").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "reserved internal `export` run kind is deliberately absent" in overview
    assert re.search(
        r"^\| Repository generation .* \| external provider\s+\|$",
        overview,
        flags=re.MULTILINE,
    )
    assert "`POST /api/v1/runs` route and are dispatched durably" in runtimes
    assert "`generate-llm` and `generate-repository`" in runtimes
    assert "advanced-capabilities.v1" not in overview + runtimes
    assert "execution_routes_exposed" not in overview + runtimes
    assert "Hugging Face repository imports share the" in compose
