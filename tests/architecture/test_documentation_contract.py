"""Repository documentation integrity contracts."""

from __future__ import annotations

import ast
import inspect
import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import corpusgen
import pytest
from pydantic import TypeAdapter

from corpuskit.api.jobs import JobSubmissionRequest
from corpuskit.api.workflows import EvaluationRequest, G2PRequest, SelectionHttpRequest
from corpuskit.domain.cli_parity import CliPreviewRequest
from corpuskit.domain.generation import RepositoryGenerationRequest
from corpuskit.domain.multilingual_demo import MultilingualDemoRequest
from corpuskit.domain.workspaces import (
    ManualCorpusInput,
    ManualCorpusVersionInput,
    ProjectInput,
)
from corpuskit.workflows.handlers import EvaluateRunSpec

ROOT = Path(__file__).parents[2]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)]+)\)")
RECIPE_REQUEST = re.compile(
    r"<!--\s*recipe-request:(?P<name>[a-z0-9-]+)\s*-->\s*"
    r"```json\s*(?P<payload>.*?)\s*```",
    flags=re.DOTALL,
)
PYTHON_FENCE = re.compile(r"```python\s*(?P<source>.*?)\s*```", flags=re.DOTALL)
MARKDOWN_HEADING = re.compile(r"^#{1,6}[ \t]+(?P<label>.+?)[ \t]*#*[ \t]*$", flags=re.MULTILINE)


def _markdown_heading_anchors(document: Path) -> set[str]:
    """Return the GitHub-style anchors used by the repository's plain Markdown headings."""

    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    for match in MARKDOWN_HEADING.finditer(document.read_text(encoding="utf-8")):
        label = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", match["label"])
        label = re.sub(r"[`*_~]", "", label).lower()
        base = re.sub(r"[^\w\- ]", "", label)
        base = re.sub(r"\s+", "-", base.strip())
        occurrence = occurrences.get(base, 0)
        occurrences[base] = occurrence + 1
        anchors.add(base if occurrence == 0 else f"{base}-{occurrence}")
    return anchors


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
        if target.scheme or target.netloc:
            continue
        relative_path = unquote(target.path)
        if relative_path:
            resolved = (
                (ROOT / relative_path.lstrip("/"))
                if relative_path.startswith("/")
                else (document.parent / relative_path)
            )
        else:
            resolved = document
        if not resolved.resolve().is_relative_to(ROOT.resolve()):
            failures.append(f"link escapes repository: {raw_target}")
        elif not resolved.exists():
            failures.append(f"missing target: {raw_target}")
        elif target.fragment and resolved.suffix.lower() == ".md":
            fragment = unquote(target.fragment).lower()
            if fragment not in _markdown_heading_anchors(resolved):
                failures.append(f"missing heading: {raw_target}")

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


def test_getting_started_contract_tracks_the_runnable_local_stack() -> None:
    """Keep the beginner copy/paste path aligned with deployable files and Compose."""

    guide = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    web_environment = (ROOT / "apps" / "web" / ".env.example").read_text(encoding="utf-8")

    required_files = (
        "compose.yaml",
        "docker/api.Dockerfile",
        "docker/web.Dockerfile",
        "docker/worker.Dockerfile",
        "docker/mutation.Dockerfile",
        "pyproject.toml",
        "uv.lock",
        "package.json",
        "apps/web/package.json",
        "package-lock.json",
        ".env.example",
        "apps/web/.env.example",
    )
    assert not [path for path in required_files if not (ROOT / path).is_file()]

    required_guide_fragments = (
        "docker compose --profile web up --build --detach --wait",
        '"ready": true',
        "http://127.0.0.1:3000/projects",
        "http://127.0.0.1:8000/api/v1/health/ready",
        "docker compose --profile web down",
        "docker compose --profile web down --volumes --remove-orphans",
        "uv sync --frozen --all-groups",
        "CORPUSKIT_DATABASE_URL=sqlite+aiosqlite:///./data/corpuskit.db",
        '$env:CORPUSKIT_DATABASE_URL = "sqlite+aiosqlite:///./data/corpuskit.db"',
        "apps/web/.env.local",
        "The migration CLI deliberately reads only the process",
        "same demo owner",
    )
    assert not [fragment for fragment in required_guide_fragments if fragment not in guide]

    for service in (
        "postgres",
        "minio",
        "minio-init",
        "api",
        "migrate",
        "provision-phoible",
        "web",
    ):
        assert re.search(rf"^  {re.escape(service)}:$", compose, flags=re.MULTILINE)
    assert "profiles: [web]" in compose

    for variable in (
        "CORPUSKIT_ENVIRONMENT",
        "CORPUSKIT_API_INTERNAL_URL",
        "CORPUSKIT_WEB_AUTH_MODE",
        "CORPUSKIT_WEB_STATE_SECRET",
        "CORPUSKIT_WEB_SESSION_ENCRYPTION_KEYS",
        "CORPUSKIT_WEB_ALLOWED_RETURN_PATHS",
    ):
        assert re.search(rf"^{variable}=.+$", web_environment, flags=re.MULTILINE)


def _validate_durable_evaluation(payload: object) -> object:
    request = JobSubmissionRequest.model_validate(payload)
    assert request.kind.value == "evaluate"
    return EvaluateRunSpec.model_validate(request.spec)


def test_recipe_request_bodies_match_the_application_contracts() -> None:
    """Make every marked copy/paste request fail CI when its DTO changes."""

    cookbook = (ROOT / "docs" / "recipes.md").read_text(encoding="utf-8")
    matches = tuple(RECIPE_REQUEST.finditer(cookbook))
    names = tuple(match["name"] for match in matches)
    validators: dict[str, Callable[[object], Any]] = {
        "project-create": ProjectInput.model_validate,
        "corpus-create": ManualCorpusInput.model_validate,
        "corpus-append": ManualCorpusVersionInput.model_validate,
        "g2p": G2PRequest.model_validate,
        "evaluate": EvaluationRequest.model_validate,
        "select": SelectionHttpRequest.model_validate,
        "repository-preview": RepositoryGenerationRequest.model_validate,
        "durable-evaluate": _validate_durable_evaluation,
        "cli-preview-evaluate": TypeAdapter(CliPreviewRequest).validate_python,
        "multilingual-demo": MultilingualDemoRequest.model_validate,
    }

    assert len(names) == len(set(names))
    assert set(names) == set(validators)
    for match in matches:
        payload = json.loads(match["payload"])
        validators[match["name"]](payload)


def test_recipe_routes_exist_in_the_committed_openapi_contract() -> None:
    """Keep the cookbook on mounted public paths rather than router-local paths."""

    document = json.loads((ROOT / "contracts" / "openapi.json").read_text(encoding="utf-8"))
    paths = set(document["paths"])
    assert {
        "/api/v1/version",
        "/api/v1/capabilities",
        "/api/v1/health/ready",
        "/api/v1/projects",
        "/api/v1/projects/{project_id}/corpora",
        "/api/v1/projects/{project_id}/corpora/{corpus_id}/versions",
        ("/api/v1/projects/{project_id}/corpora/{corpus_id}/versions/{version_id}/export"),
        "/api/v1/g2p",
        "/api/v1/g2p/batch",
        "/api/v1/phonology/inventories/{identifier}",
        "/api/v1/phonology/inventories/{identifier}/sources",
        "/api/v1/evaluations",
        "/api/v1/selections",
        "/api/v1/generation/preview",
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
        "/api/v1/runs/{run_id}/events",
        "/api/v1/runs/{run_id}/cancellation",
        "/api/v1/runs/{run_id}/retries",
        "/api/v1/labs/cli/preview",
        "/api/v1/labs/demos/multilingual",
    } <= paths


def test_corpusgen_relationship_tracks_the_exact_dependency_contract() -> None:
    """Keep the decision guide, pin, and no-sibling-checkout claim synchronized."""

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    specifications = [
        specification
        for specification in (
            *project["dependencies"],
            *(item for group in project["optional-dependencies"].values() for item in group),
        )
        if specification.startswith("corpusgen")
    ]
    matches = [
        re.fullmatch(r"corpusgen(?:\[[^]]+\])?==(?P<version>[0-9]+(?:\.[0-9]+)+)", item)
        for item in specifications
    ]
    assert specifications
    assert all(matches)
    versions = {match["version"] for match in matches if match is not None}
    assert len(versions) == 1
    version = versions.pop()

    relationship = (ROOT / "docs" / "corpusgen-relationship.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    getting_started = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    docs_home = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    capability_matrix = (ROOT / "docs" / "product" / "capability-matrix.md").read_text(
        encoding="utf-8"
    )
    operations_map = (ROOT / "docs" / "product" / "capability-operations.md").read_text(
        encoding="utf-8"
    )
    locked_versions = {
        package["version"]
        for package in tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
        if package["name"] == "corpusgen"
    }

    assert locked_versions == {version}
    assert f"`corpusgen=={version}`" in relationship
    assert f"corpusgen/tree/v{version}" in relationship
    assert f"corpusgen/{version}/" in relationship
    assert "sibling checkout or an editable path" in relationship
    assert "CorpusKit depends on CorpusGen, never the reverse" in relationship
    assert "bounded synchronous service" in relationship
    assert "durable submission, policy, quota, and transactional outbox" in relationship
    assert "does not automatically create a run" in relationship
    for document in (readme, capability_matrix):
        assert f"corpusgen=={version}" in document
        assert f"corpusgen/tree/v{version}" in document
    assert "docs/corpusgen-relationship.md" in readme
    assert "docs/recipes.md" in readme
    assert "(corpusgen-relationship.md)" in getting_started + docs_home
    assert "(recipes.md)" in getting_started + docs_home
    assert "(../recipes.md)" in operations_map


def test_documented_corpusgen_python_example_matches_the_installed_api() -> None:
    relationship = (ROOT / "docs" / "corpusgen-relationship.md").read_text(encoding="utf-8")
    snippets = tuple(match["source"] for match in PYTHON_FENCE.finditer(relationship))

    assert snippets
    for source in snippets:
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "corpusgen"
            and node.func.attr == "evaluate"
        ]
        assert len(calls) == 1
        call = calls[0]
        assert all(keyword.arg is not None for keyword in call.keywords)
        inspect.signature(corpusgen.evaluate).bind(
            *([object()] * len(call.args)),
            **{keyword.arg: object() for keyword in call.keywords if keyword.arg is not None},
        )

        result_names = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and node.value is call
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        documented_attributes = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in result_names
        }
        result_type = inspect.get_annotations(corpusgen.evaluate, eval_str=True)["return"]
        result_fields = set(getattr(result_type, "__dataclass_fields__", {}))
        assert documented_attributes <= result_fields


def test_obsolete_documentation_claims_do_not_return() -> None:
    overview = (ROOT / "docs" / "architecture" / "overview.md").read_text(encoding="utf-8")
    adr = (ROOT / "docs" / "adr" / "0001-corpusgen-adapter-boundary.md").read_text(encoding="utf-8")
    getting_started = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    live_demo = (ROOT / "docs" / "product" / "15-minute-demo.md").read_text(encoding="utf-8")
    environment_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "CLI/Python recipes" not in overview + adr
    assert "equivalent CLI and Python recipes" not in overview + adr
    assert "generated API client" not in overview
    assert "src/corpuskit/security/" not in overview
    assert "infra/" not in overview
    assert "optional `durable` profile" in overview
    assert "does not execute it" in overview
    assert "it has no dispatcher or worker" in overview
    assert "inline job flow" not in getting_started
    assert "in-process job backend" not in getting_started
    assert "identity, and inline jobs" not in getting_started
    assert "core demo uses the inline backend" not in live_demo
    assert "development may use the in-process runner" not in environment_example
    assert "remains queued" in getting_started + live_demo
