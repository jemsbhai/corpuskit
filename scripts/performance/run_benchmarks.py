"""Run the bounded CorpusKit performance fixture and emit auditable JSON evidence."""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI

from corpuskit import __version__
from corpuskit.adapters.corpusgen import CorpusgenAdapter, CorpusgenInventoryAdapter
from corpuskit.api.exploration_analysis import exploration_analysis_router
from corpuskit.api.jobs import job_router
from corpuskit.auth import AuthRole, DemoAuthenticator, require_roles
from corpuskit.config import Settings
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.workspaces import CorpusExportFormat
from corpuskit.persistence.models import OutboxState
from corpuskit.services.exploration_analysis import InventoryExplorationService
from corpuskit.services.jobs import (
    EventSnapshot,
    JobActor,
    RunSnapshot,
    RunSubmission,
    SubmissionResult,
)
from corpuskit.services.project_workspaces import SentenceSnapshot, VersionSnapshot, build_export
from corpuskit.workflows.handlers import build_core_handler_registry
from corpuskit.workflows.process_runner import ProcessExecutionRunner
from scripts.performance.benchmark_contract import PROFILE_SCHEMA, RESULT_SCHEMA, summarize

FIXTURE_SCHEMA = "corpuskit.performance-fixture.v1"
MEMORY_SCHEMA = "corpuskit.worker-memory-result.v1"
_RUN_ID = UUID("30000000-0000-4000-8000-000000000001")
_ORG_ID = UUID("00000000-0000-4000-8000-000000000001")
_PROJECT_ID = UUID("00000000-0000-4000-8000-000000000003")
_CORPUS_ID = UUID("30000000-0000-4000-8000-000000000002")
_VERSION_ID = UUID("30000000-0000-4000-8000-000000000003")
_CREATED_AT = datetime(2026, 8, 11, tzinfo=UTC)


class BenchmarkFixtureError(ValueError):
    """Raised when the declared load fixture is incomplete or unbounded."""


class _UnusedAnalysisService:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"performance fixture unexpectedly requested analysis method {name}")


class _StaticJobService:
    """No-I/O service seam: this benchmark measures HTTP/control serialization only."""

    def __init__(self) -> None:
        self.snapshot = RunSnapshot(
            id=_RUN_ID,
            organization_id=_ORG_ID,
            project_id=_PROJECT_ID,
            corpus_version_id=None,
            parent_run_id=None,
            kind=RunKind.EVALUATE,
            state=RunState.SUCCEEDED,
            attempt=1,
            spec={"fixture": "performance-v1"},
            spec_sha256="a" * 64,
            outbox_state=OutboxState.PUBLISHED,
            cancellation_requested_at=None,
            created_at=_CREATED_AT,
            result_summary={"coverage": 1.0},
            failure_code=None,
        )

    async def submit(
        self,
        actor: JobActor,
        submission: RunSubmission,
        *,
        idempotency_key: str,
    ) -> SubmissionResult:
        del actor, submission, idempotency_key
        await asyncio.sleep(0)
        return SubmissionResult(run=self.snapshot, created=True)

    async def get(self, actor: JobActor, run_id: UUID) -> RunSnapshot:
        del actor
        await asyncio.sleep(0)
        if run_id != _RUN_ID:
            raise AssertionError("unexpected run ID")
        return self.snapshot

    async def list(
        self,
        actor: JobActor,
        *,
        state: RunState | None,
        kind: RunKind | None,
        offset: int,
        limit: int,
    ) -> tuple[RunSnapshot, ...]:
        del actor, state, kind, offset, limit
        await asyncio.sleep(0)
        return (self.snapshot,)

    async def events(
        self,
        actor: JobActor,
        run_id: UUID,
        *,
        after: int,
        limit: int,
    ) -> tuple[EventSnapshot, ...]:
        del actor, run_id, after, limit
        await asyncio.sleep(0)
        return ()

    async def request_cancellation(self, actor: JobActor, run_id: UUID) -> RunSnapshot:
        del actor, run_id
        await asyncio.sleep(0)
        return self.snapshot

    async def retry(
        self,
        actor: JobActor,
        run_id: UUID,
        *,
        idempotency_key: str,
    ) -> SubmissionResult:
        del actor, run_id, idempotency_key
        await asyncio.sleep(0)
        return SubmissionResult(run=self.snapshot, created=True)


def load_fixture(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkFixtureError("performance fixture is not readable JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != FIXTURE_SCHEMA:
        raise BenchmarkFixtureError("unsupported performance fixture schema")
    language = value.get("language")
    evaluation = value.get("evaluation")
    selection = value.get("selection")
    export = value.get("export")
    memory = value.get("memory")
    if not isinstance(language, str) or not language:
        raise BenchmarkFixtureError("fixture language is required")
    if not all(isinstance(item, dict) for item in (evaluation, selection, export, memory)):
        raise BenchmarkFixtureError("fixture workload sections are required")
    evaluation = cast(dict[str, Any], evaluation)
    selection = cast(dict[str, Any], selection)
    export = cast(dict[str, Any], export)
    memory = cast(dict[str, Any], memory)
    sentences = evaluation.get("sentences")
    repetitions = evaluation.get("repetitions")
    if (
        not isinstance(sentences, list)
        or not sentences
        or not all(isinstance(item, str) and item.strip() for item in sentences)
        or not isinstance(repetitions, int)
        or isinstance(repetitions, bool)
        or len(sentences) * repetitions != 100
    ):
        raise BenchmarkFixtureError("evaluation fixture must expand to exactly 100 sentences")
    _exact_positive_integer(selection, "candidate_count", 1_000)
    _bounded_positive_integer(selection, "max_sentences", maximum=2_000)
    target_units = selection.get("target_units")
    if (
        not isinstance(target_units, list)
        or not target_units
        or len(target_units) != len(set(target_units))
        or not all(isinstance(item, str) and item.strip() for item in target_units)
    ):
        raise BenchmarkFixtureError("selection target units must be unique strings")
    _exact_positive_integer(export, "sentence_count", 10_000)
    if export.get("format") != "json" or "{ordinal}" not in str(
        export.get("sentence_template", "")
    ):
        raise BenchmarkFixtureError("export fixture must declare the deterministic JSON template")
    _exact_positive_integer(memory, "job_count", 100)
    _bounded_positive_integer(memory, "sentences_per_job", maximum=100)
    _bounded_positive_integer(memory, "warmup_jobs", maximum=20)
    canonical = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return value, hashlib.sha256(canonical).hexdigest()


def _exact_positive_integer(section: dict[str, Any], key: str, expected: int) -> None:
    value = section.get(key)
    if value != expected or isinstance(value, bool):
        raise BenchmarkFixtureError(f"{key} must equal {expected}")


def _bounded_positive_integer(section: dict[str, Any], key: str, *, maximum: int) -> None:
    value = section.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise BenchmarkFixtureError(f"{key} must be between one and {maximum}")


def expanded_evaluation_sentences(fixture: dict[str, Any]) -> tuple[str, ...]:
    section = fixture["evaluation"]
    return tuple(section["sentences"] * section["repetitions"])


def selection_inputs(
    fixture: dict[str, Any],
) -> tuple[list[str], list[list[str]], set[str], int]:
    section = fixture["selection"]
    targets = list(section["target_units"])
    count = int(section["candidate_count"])
    candidates = [f"prephonemized-{index:04d}" for index in range(count)]
    phonemes = [
        [targets[index % len(targets)], targets[(index * 5 + 3) % len(targets)]]
        for index in range(count)
    ]
    return candidates, phonemes, set(targets), int(section["max_sentences"])


def export_inputs(
    fixture: dict[str, Any],
) -> tuple[VersionSnapshot, tuple[SentenceSnapshot, ...]]:
    section = fixture["export"]
    count = int(section["sentence_count"])
    template = str(section["sentence_template"])
    sentences = tuple(
        SentenceSnapshot(
            ordinal=index,
            original_text=template.format(ordinal=index),
            normalized_text=template.format(ordinal=index),
        )
        for index in range(count)
    )
    version = VersionSnapshot(
        id=_VERSION_ID,
        corpus_id=_CORPUS_ID,
        parent_version_id=None,
        version_number=1,
        language=str(fixture["language"]),
        sentence_count=count,
        content_sha256="b" * 64,
        corpusgen_version=importlib.metadata.version("corpusgen"),
        created_at=_CREATED_AT,
    )
    return version, sentences


def has_monotonic_memory_growth(samples: tuple[int, ...], baseline: int) -> bool:
    """Flag material, uninterrupted RSS growth while ignoring allocator-scale noise."""

    if baseline <= 0 or len(samples) < 3 or any(value <= 0 for value in samples):
        raise BenchmarkFixtureError("memory evidence requires positive baseline and samples")
    material_growth = samples[-1] > baseline * 1.02
    never_decreased = all(right >= left for left, right in pairwise(samples))
    return material_growth and never_decreased


async def _measure_async(
    operation: Callable[[], Awaitable[None]], *, warmups: int, samples: int
) -> tuple[float, ...]:
    for _ in range(warmups):
        await operation()
    durations: list[float] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        await operation()
        durations.append((time.perf_counter_ns() - started) / 1_000_000_000)
    return tuple(durations)


def _measure_sync(
    operation: Callable[[], None], *, warmups: int, samples: int
) -> tuple[float, ...]:
    for _ in range(warmups):
        operation()
    durations: list[float] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        operation()
        durations.append((time.perf_counter_ns() - started) / 1_000_000_000)
    return tuple(durations)


def _benchmark_app() -> FastAPI:
    app = FastAPI()
    app.state.authenticator = DemoAuthenticator()
    inventory = InventoryExplorationService(CorpusgenInventoryAdapter())
    read_roles = Depends(
        require_roles(AuthRole.OWNER, AuthRole.ADMIN, AuthRole.EDITOR, AuthRole.VIEWER)
    )
    app.include_router(
        exploration_analysis_router(inventory, cast(Any, _UnusedAnalysisService())),
        prefix="/api/v1",
        dependencies=[read_roles],
    )
    app.include_router(job_router(_StaticJobService()), prefix="/api/v1")
    return app


async def run_benchmarks(
    fixture: dict[str, Any],
    *,
    fixture_sha256: str,
    profile_id: str,
    samples: int,
    warmups: int,
    include_memory: bool,
) -> dict[str, object]:
    if not 5 <= samples <= 200 or not 1 <= warmups <= 20:
        raise BenchmarkFixtureError("samples must be 5..200 and warmups must be 1..20")
    adapter = CorpusgenAdapter()
    evaluation_sentences = expanded_evaluation_sentences(fixture)
    candidates, candidate_phonemes, target_units, max_sentences = selection_inputs(fixture)
    version, export_sentences = export_inputs(fixture)

    from corpusgen.select.greedy import GreedySelector

    selector = GreedySelector(unit="phoneme")
    app = _benchmark_app()
    transport = httpx.ASGITransport(app=app)
    limits: dict[str, dict[str, float]] = {
        "cached_inventory_search_http": {"p95": 0.4, "p99": 1.0},
        "cached_phonology_status_http": {"p95": 0.4, "p99": 1.0},
        "job_status_http_no_persistence": {"p95": 0.4, "p99": 1.0},
        "job_submission_http_no_persistence": {"p95": 0.3},
        "evaluate_100_sentences": {"p95": 10.0},
        "greedy_select_1000_prephonemized": {"p95": 15.0},
        "export_10000_sentences_json": {"p95": 10.0},
    }
    measurements: dict[str, tuple[float, ...]] = {}

    async with httpx.AsyncClient(
        transport=transport, base_url="http://performance.local"
    ) as client:

        async def inventory_search() -> None:
            response = await client.get(
                "/api/v1/phonology/languages", params={"query": "English", "limit": 50}
            )
            if response.status_code != 200 or response.json().get("total", 0) < 1:
                raise RuntimeError("the checksum-verified PHOIBLE cache is not ready")

        async def phonology_status() -> None:
            response = await client.get("/api/v1/phonology/status")
            body = response.json()
            if (
                response.status_code != 200
                or not body.get("cache_available")
                or not body.get("loaded")
            ):
                raise RuntimeError("the cached PHOIBLE status endpoint is not ready")

        async def job_status() -> None:
            response = await client.get(f"/api/v1/runs/{_RUN_ID}")
            if response.status_code != 200 or response.json().get("id") != str(_RUN_ID):
                raise RuntimeError("job-status benchmark returned an invalid response")

        async def job_submission() -> None:
            response = await client.post(
                "/api/v1/runs",
                headers={"Idempotency-Key": "performance-fixture-v1"},
                json={
                    "project_id": str(_PROJECT_ID),
                    "kind": "evaluate",
                    "spec": {"fixture": "performance-v1"},
                },
            )
            if response.status_code != 201 or response.json().get("id") != str(_RUN_ID):
                raise RuntimeError("job-submission benchmark returned an invalid response")

        # Load and validate PHOIBLE before timing the explicitly cached endpoint cases.
        await inventory_search()
        async_operations: tuple[tuple[str, Callable[[], Awaitable[None]]], ...] = (
            ("cached_inventory_search_http", inventory_search),
            ("cached_phonology_status_http", phonology_status),
            ("job_status_http_no_persistence", job_status),
            ("job_submission_http_no_persistence", job_submission),
        )
        for async_name, async_operation in async_operations:
            measurements[async_name] = await _measure_async(
                async_operation, warmups=warmups, samples=samples
            )

    def evaluate() -> None:
        result = adapter.evaluate(evaluation_sentences, language=str(fixture["language"]))
        if result.total_sentences != 100:
            raise RuntimeError("evaluation benchmark did not process exactly 100 sentences")

    def select() -> None:
        result = selector.select(
            candidates,
            candidate_phonemes,
            target_units,
            max_sentences=max_sentences,
            target_coverage=1.0,
        )
        if result.coverage != 1.0 or not result.selected_indices:
            raise RuntimeError("selection benchmark did not produce complete coverage")

    def export() -> None:
        result = build_export(
            corpus_id=_CORPUS_ID,
            corpus_name="Performance fixture",
            version=version,
            sentences=export_sentences,
            export_format=CorpusExportFormat.JSON,
        )
        if not result.content or result.sha256 != hashlib.sha256(result.content).hexdigest():
            raise RuntimeError("export benchmark integrity check failed")

    sync_operations: tuple[tuple[str, Callable[[], None]], ...] = (
        ("evaluate_100_sentences", evaluate),
        ("greedy_select_1000_prephonemized", select),
        ("export_10000_sentences_json", export),
    )
    for sync_name, sync_operation in sync_operations:
        measurements[sync_name] = _measure_sync(sync_operation, warmups=warmups, samples=samples)

    memory = (
        await _memory_probe(fixture, evaluation_sentences)
        if include_memory
        else {"schema_version": MEMORY_SCHEMA, "measured": False, "reason": "disabled"}
    )
    benchmarks: dict[str, object] = {}
    scopes = {
        "cached_inventory_search_http": "in-process ASGI; real checksum-verified cached PHOIBLE",
        "cached_phonology_status_http": "in-process ASGI; real loaded PHOIBLE status",
        "job_status_http_no_persistence": (
            "in-process ASGI/control serialization; static service seam"
        ),
        "job_submission_http_no_persistence": (
            "in-process ASGI/auth/validation/control serialization; static service seam"
        ),
        "evaluate_100_sentences": "CorpusKit adapter; real CorpusGen and eSpeak",
        "greedy_select_1000_prephonemized": "public CorpusGen greedy selector; no G2P",
        "export_10000_sentences_json": "CorpusKit deterministic JSON export encoder",
    }
    for name, durations in measurements.items():
        benchmarks[name] = {
            **summarize(durations),
            "limits_seconds": limits[name],
            "scope": scopes[name],
        }
    return {
        "schema_version": RESULT_SCHEMA,
        "suite_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "source": _source_metadata(),
        "environment": _environment(profile_id),
        "measurement_protocol": {
            "clock": "perf_counter_ns",
            "percentile_method": "nearest_rank",
            "samples_per_benchmark": samples,
            "warmups_per_benchmark": warmups,
        },
        "fixture": {
            "schema_version": fixture["schema_version"],
            "sha256": fixture_sha256,
        },
        "benchmarks": benchmarks,
        "worker_memory": memory,
        "unmeasured_acceptance_targets": [
            "cpu_queue_start_under_provisioned_load",
            "gpu_queue_start_under_provisioned_load",
            "postgres_job_status_and_submission",
            "core_web_vitals",
            "initial_javascript_gzip",
            "24_hour_staging_soak",
        ],
    }


async def _memory_probe(
    fixture: dict[str, Any],
    evaluation_sentences: tuple[str, ...],
) -> dict[str, object]:
    try:
        psutil = importlib.import_module("psutil")
    except ImportError:
        return {
            "schema_version": MEMORY_SCHEMA,
            "measured": False,
            "reason": "psutil_not_installed",
        }
    section = fixture["memory"]
    batch = evaluation_sentences[: int(section["sentences_per_job"])]
    settings = Settings(
        environment="test",
        runtime_role="worker",
        worker_profile="batch-cpu",
        temporal_task_queue="batch-cpu",
        _env_file=None,
    )
    runner = ProcessExecutionRunner(
        build_core_handler_registry(settings),
        hard_timeout_seconds=60.0,
    )
    spec = {"sentences": list(batch), "language": str(fixture["language"])}

    async def tick() -> None:
        return None

    async def job() -> None:
        result = await runner.execute(
            RunKind.EVALUATE,
            spec,
            tick=tick,
            tick_seconds=0.25,
        )
        if result.get("total_sentences") != len(batch):
            raise RuntimeError("memory fixture job returned an incomplete result")

    for _ in range(int(section["warmup_jobs"])):
        await job()
    gc.collect()
    process = psutil.Process()
    baseline = int(process.memory_info().rss)
    samples: list[int] = []
    job_count = int(section["job_count"])
    for index in range(job_count):
        await job()
        if (index + 1) % 10 == 0:
            gc.collect()
            samples.append(int(process.memory_info().rss))
    final = samples[-1]
    growth = (final - baseline) / baseline
    monotonic = has_monotonic_memory_growth(tuple(samples), baseline)
    return {
        "schema_version": MEMORY_SCHEMA,
        "measured": True,
        "jobs": job_count,
        "sentences_per_job": len(batch),
        "sample_every_jobs": 10,
        "post_warm_rss_bytes": baseline,
        "rss_samples_bytes": samples,
        "final_rss_bytes": final,
        "growth_ratio": growth,
        "within_ten_percent": final <= baseline * 1.10,
        "monotonic_growth": monotonic,
        "passed": final <= baseline * 1.10 and not monotonic,
        "scope": (
            "production parent worker RSS; 100 killable child-process jobs with real "
            "CorpusKit evaluation/CorpusGen/eSpeak"
        ),
    }


def _source_metadata() -> dict[str, object]:
    git = shutil.which("git")
    if git is None:
        return {
            "git_revision": "uncommitted",
            "git_dirty": True,
            "corpuskit_version": __version__,
            "corpusgen_version": importlib.metadata.version("corpusgen"),
        }
    try:
        revision = subprocess.run(  # noqa: S603 - executable resolved from the local PATH
            [git, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = "uncommitted"
    try:
        dirty = bool(
            subprocess.run(  # noqa: S603 - executable resolved from the local PATH
                [git, "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        dirty = True
    return {
        "git_revision": revision,
        "git_dirty": dirty,
        "corpuskit_version": __version__,
        "corpusgen_version": importlib.metadata.version("corpusgen"),
    }


def _environment(profile_id: str) -> dict[str, object]:
    return {
        "profile_schema_version": PROFILE_SCHEMA,
        "profile_id": profile_id,
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "cpu_model": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "unknown"),
        "logical_cpu_count": os.cpu_count(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/performance/v1.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--skip-memory", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.profile_id.strip() or len(arguments.profile_id) > 128:
        raise BenchmarkFixtureError("profile ID must be a non-empty bounded label")
    fixture, digest = load_fixture(arguments.fixture)
    result = asyncio.run(
        run_benchmarks(
            fixture,
            fixture_sha256=digest,
            profile_id=arguments.profile_id,
            samples=arguments.samples,
            warmups=arguments.warmups,
            include_memory=not arguments.skip_memory,
        )
    )
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by scheduled automation
    sys.exit(main())
