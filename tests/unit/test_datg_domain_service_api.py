from __future__ import annotations

import socket
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from corpuskit.adapters.corpusgen.datg import CorpusgenDatgBindings
from corpuskit.api.datg_lab import datg_lab_router
from corpuskit.auth.verifier import DemoAuthenticator
from corpuskit.domain.datg import (
    DatgAntiMode,
    DatgCacheIdentity,
    DatgCoveredInspectionRequest,
    DatgFrequencyInspectionRequest,
    DatgGeneratedCandidate,
    DatgGuidanceManifest,
    DatgGuidanceOptions,
    DatgGuidedGenerationRequest,
    DatgGuidedGenerationResult,
    DatgIndexArtifact,
    DatgIndexBuildRequest,
    DatgIndexedToken,
    DatgIndexPublication,
    DatgInspectionResult,
    DatgLogitDeltaPreviewRequest,
    DatgLogitDeltaPreviewResult,
    DatgLogitPreviewRequest,
    DatgLogitPreviewResult,
    DatgPhonemeSequence,
    DatgQuantization,
    DatgRuntimePolicyEntry,
    DatgSnapshotPin,
    DatgTargetInspectionRequest,
    DatgTokenMatch,
    DatgUnit,
    DatgUnitFrequency,
    DatgUnitTokenSet,
    DatgWorkerProfile,
)
from corpuskit.domain.errors import EngineUnavailableError, InvalidRequestError
from corpuskit.services.datg import DatgCoordinator, DatgInspectionService, DatgRuntimePolicy
from corpuskit.services.datg_catalog import DatgCatalogActor
from corpuskit.services.rate_limits import DisabledRateLimiter

PROJECT_ID = UUID("00000000-0000-4000-8000-000000000003")


def pin(*, digest: str = "b" * 64) -> DatgSnapshotPin:
    return DatgSnapshotPin(
        repository_id="acme/tiny-datg",
        revision="a" * 40,
        snapshot_sha256=digest,
    )


def policy_entry() -> DatgRuntimePolicyEntry:
    model_pin = pin()
    return DatgRuntimePolicyEntry(
        runtime_id="tiny-datg",
        model=model_pin,
        tokenizer=model_pin,
        allowed_quantizations=(DatgQuantization.NONE, DatgQuantization.FOUR_BIT),
    )


def artifact(unit: DatgUnit = DatgUnit.PHONEME) -> DatgIndexArtifact:
    values = {
        DatgUnit.PHONEME: ("b", "p"),
        DatgUnit.DIPHONE: ("b-t", "p-b"),
        DatgUnit.TRIPHONE: ("b-t-k", "p-b-t"),
    }[unit]
    identity = DatgCacheIdentity.create(
        tokenizer=pin(),
        language="en-us",
        unit=unit,
        corpusgen_version="0.1.7",
        espeak_version="1.52.0",
    )
    return DatgIndexArtifact.create(
        identity=identity,
        vocabulary_size=3,
        unit_to_tokens=(
            DatgUnitTokenSet(unit=values[0], token_ids=(1, 2)),
            DatgUnitTokenSet(unit=values[1], token_ids=(0, 2)),
        ),
        token_units=(
            DatgIndexedToken(token_id=0, decoded_text="first", units=(values[1],)),
            DatgIndexedToken(token_id=1, decoded_text="second", units=(values[0],)),
            DatgIndexedToken(token_id=2, decoded_text="both", units=tuple(sorted(values))),
        ),
    )


class MemoryCache:
    def __init__(self, value: DatgIndexArtifact | None, *, fail: bool = False) -> None:
        self.value = value
        self.fail = fail

    def get(self, cache_key_sha256: str) -> DatgIndexArtifact | None:
        del cache_key_sha256
        if self.fail:
            raise RuntimeError("C:/private/cache and token=secret")
        return self.value


class RecordingEngine:
    def __init__(self) -> None:
        self.build_calls = 0
        self.generate_calls = 0

    def build_index(self, request: object, policy: object) -> object:
        del request, policy
        self.build_calls += 1
        raise RuntimeError("worker path /secret")

    def generate(
        self, request: object, policy: object, profile: object, artifact: object
    ) -> object:
        del request, policy, profile, artifact
        self.generate_calls += 1
        raise RuntimeError("worker path /secret")


def generation_request(value: DatgIndexArtifact | None = None) -> DatgGuidedGenerationRequest:
    value = value or artifact()
    return DatgGuidedGenerationRequest(
        runtime_id="tiny-datg",
        index_cache_key_sha256=value.identity.cache_key_sha256,
        target_phonemes=("p", "b"),
        target_units=("b",),
        coverage_sequences=(DatgPhonemeSequence(phonemes=("p",)),),
        guidance=DatgGuidanceOptions(anti_attribute_mode=DatgAntiMode.COVERED),
        candidates=2,
        max_new_tokens=16,
        seed=7,
    )


def test_cache_identity_key_binds_every_required_dimension() -> None:
    base = DatgCacheIdentity.create(
        tokenizer=pin(),
        language="en-us",
        unit=DatgUnit.PHONEME,
        corpusgen_version="0.1.7",
        espeak_version="1.52.0",
    )
    variants = (
        DatgCacheIdentity.create(
            tokenizer=DatgSnapshotPin(
                repository_id="acme/other",
                revision="a" * 40,
                snapshot_sha256="b" * 64,
            ),
            language="en-us",
            unit=DatgUnit.PHONEME,
            corpusgen_version="0.1.7",
            espeak_version="1.52.0",
        ),
        DatgCacheIdentity.create(
            tokenizer=DatgSnapshotPin(
                repository_id="acme/tiny-datg",
                revision="c" * 40,
                snapshot_sha256="b" * 64,
            ),
            language="en-us",
            unit=DatgUnit.PHONEME,
            corpusgen_version="0.1.7",
            espeak_version="1.52.0",
        ),
        DatgCacheIdentity.create(
            tokenizer=pin(digest="d" * 64),
            language="en-us",
            unit=DatgUnit.PHONEME,
            corpusgen_version="0.1.7",
            espeak_version="1.52.0",
        ),
        DatgCacheIdentity.create(
            tokenizer=pin(),
            language="fr-fr",
            unit=DatgUnit.PHONEME,
            corpusgen_version="0.1.7",
            espeak_version="1.52.0",
        ),
        DatgCacheIdentity.create(
            tokenizer=pin(),
            language="en-us",
            unit=DatgUnit.DIPHONE,
            corpusgen_version="0.1.7",
            espeak_version="1.52.0",
        ),
        DatgCacheIdentity.create(
            tokenizer=pin(),
            language="en-us",
            unit=DatgUnit.PHONEME,
            corpusgen_version="0.1.8",
            espeak_version="1.52.0",
        ),
        DatgCacheIdentity.create(
            tokenizer=pin(),
            language="en-us",
            unit=DatgUnit.PHONEME,
            corpusgen_version="0.1.7",
            espeak_version="1.53.0",
        ),
    )
    assert len({base.cache_key_sha256, *(item.cache_key_sha256 for item in variants)}) == 8


@pytest.mark.parametrize(
    ("model", "changes"),
    [
        (DatgSnapshotPin, {"repository_id": "../model"}),
        (DatgSnapshotPin, {"revision": "main"}),
        (DatgSnapshotPin, {"snapshot_sha256": "A" * 64}),
        (DatgRuntimePolicyEntry, {"allowed_quantizations": (DatgQuantization.NONE,) * 2}),
        (DatgIndexBuildRequest, {"language": "../../etc"}),
    ],
)
def test_pin_request_and_policy_validation_rejects_unsafe_values(
    model: type[object], changes: dict[str, object]
) -> None:
    values: dict[str, object]
    if model is DatgSnapshotPin:
        values = pin().model_dump()
    elif model is DatgRuntimePolicyEntry:
        values = policy_entry().model_dump()
    else:
        values = DatgIndexBuildRequest(runtime_id="tiny-datg").model_dump()
    values.update(changes)
    with pytest.raises(ValidationError):
        model.model_validate(values)  # type: ignore[attr-defined]


def test_policy_requires_one_snapshot_and_unique_runtime_ids() -> None:
    with pytest.raises(ValidationError, match="one snapshot"):
        DatgRuntimePolicyEntry(
            runtime_id="tiny-datg",
            model=pin(),
            tokenizer=DatgSnapshotPin(
                repository_id="acme/tokenizer",
                revision="a" * 40,
                snapshot_sha256="b" * 64,
            ),
            allowed_quantizations=(DatgQuantization.NONE,),
        )
    with pytest.raises(ValueError, match="unique"):
        DatgRuntimePolicy(
            (policy_entry(), policy_entry()), worker_profile=DatgWorkerProfile.LOCAL_CPU
        )


def test_artifact_integrity_and_bidirectional_mapping_fail_closed() -> None:
    value = artifact()
    tampered = value.model_dump(mode="json")
    tampered["content_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="integrity"):
        DatgIndexArtifact.model_validate(tampered)
    inconsistent = value.model_dump(mode="json")
    inconsistent["unit_to_tokens"][0]["token_ids"] = [1]
    inconsistent["content_sha256"] = value.content_sha256
    with pytest.raises(ValidationError, match="inconsistent"):
        DatgIndexArtifact.model_validate(inconsistent)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(indexed_token_count=2),
        lambda payload: payload.update(vocabulary_size=1),
        lambda payload: payload["unit_to_tokens"].append(payload["unit_to_tokens"][0]),
        lambda payload: (
            payload.update(indexed_token_count=4, vocabulary_size=4),
            payload["token_units"].append(payload["token_units"][0]),
        ),
        lambda payload: payload["token_units"][0].update(units=["p", "x"]),
    ],
)
def test_artifact_structural_mutations_fail_closed(mutate: object) -> None:
    payload = artifact().model_dump(mode="json")
    mutate(payload)  # type: ignore[operator]
    with pytest.raises(ValidationError, match=r"inconsistent|exceeds|sorted|unique"):
        DatgIndexArtifact.model_validate(payload)


def test_small_domain_records_reject_duplicates_bounds_and_unsafe_values() -> None:
    with pytest.raises(ValidationError, match="immutable"):
        DatgSnapshotPin(
            repository_id="acme/tiny-datg",
            revision="G" * 40,
            snapshot_sha256="b" * 64,
        )
    with pytest.raises(ValidationError, match="runtime IDs"):
        DatgRuntimePolicyEntry(**(policy_entry().model_dump() | {"runtime_id": "Tiny-DATG"}))
    with pytest.raises(ValidationError, match="runtime versions"):
        DatgCacheIdentity.create(
            tokenizer=pin(),
            language="en-us",
            unit=DatgUnit.PHONEME,
            corpusgen_version="bad/version",
            espeak_version="1.52.0",
        )
    with pytest.raises(ValidationError, match="sorted"):
        DatgUnitTokenSet(unit="p", token_ids=(2, 1))
    with pytest.raises(ValidationError, match="bounded"):
        DatgUnitTokenSet(unit="p", token_ids=(-1,))
    with pytest.raises(ValidationError, match="sorted"):
        DatgIndexedToken(token_id=0, decoded_text="p", units=("p", "p"))
    with pytest.raises(ValidationError, match="unique"):
        DatgTokenMatch(token_id=0, decoded_text="p", units=("p", "p"))
    with pytest.raises(ValidationError, match="unique"):
        DatgFrequencyInspectionRequest(
            cache_key_sha256="a" * 64,
            unit_counts=(
                DatgUnitFrequency(unit="p", count=1),
                DatgUnitFrequency(unit="p", count=2),
            ),
            threshold=0,
        )
    with pytest.raises(ValidationError, match="shared width"):
        DatgLogitDeltaPreviewRequest(
            cache_key_sha256="a" * 64,
            target_phonemes=("p",),
            target_units=("p",),
            logits=((0.0,), (0.0, 1.0)),
        )
    with pytest.raises(ValidationError, match="finite number"):
        DatgLogitDeltaPreviewRequest(
            cache_key_sha256="a" * 64,
            target_phonemes=("p",),
            target_units=("p",),
            logits=((float("inf"),),),
        )
    with pytest.raises(ValidationError, match="phonemes"):
        DatgPhonemeSequence(phonemes=(" ",))
    with pytest.raises(ValidationError, match="target phonemes"):
        DatgGuidedGenerationRequest.model_validate(
            generation_request().model_dump() | {"target_phonemes": ("p", "p")}
        )
    with pytest.raises(ValidationError, match="target units"):
        DatgGuidedGenerationRequest.model_validate(
            generation_request().model_dump() | {"target_units": ("p", "p")}
        )


def test_inspection_manifest_candidate_and_result_invariants() -> None:
    value = artifact()
    result = DatgInspectionService(MemoryCache(value)).target(
        DatgTargetInspectionRequest(
            cache_key_sha256=value.identity.cache_key_sha256,
            target_units=("b",),
        )
    )
    for update in (
        {"token_ids": (1, 1)},
        {"matches": result.matches[:1]},
        {"total_matches": 1},
        {"truncated": True},
    ):
        with pytest.raises(ValidationError):
            DatgInspectionResult.model_validate(result.model_dump() | update)

    entry = policy_entry()
    manifest = DatgGuidanceManifest(
        runtime_id=entry.runtime_id,
        model_id=entry.model.repository_id,
        model_revision=entry.model.revision,
        model_snapshot_sha256=entry.model.snapshot_sha256,
        tokenizer_id=entry.tokenizer.repository_id,
        tokenizer_revision=entry.tokenizer.revision,
        tokenizer_snapshot_sha256=entry.tokenizer.snapshot_sha256,
        index_cache_key_sha256=value.identity.cache_key_sha256,
        index_content_sha256=value.content_sha256,
        language="en-us",
        unit=DatgUnit.PHONEME,
        guidance=DatgGuidanceOptions(),
        quantization=DatgQuantization.NONE,
        seed=0,
        sampling_enabled=False,
        corpusgen_version="0.1.7",
        espeak_version="1.52.0",
    )
    with pytest.raises(ValidationError, match="manifest versions"):
        DatgGuidanceManifest.model_validate(
            manifest.model_dump() | {"corpusgen_version": "bad/version"}
        )
    with pytest.raises(ValidationError, match="content"):
        DatgGeneratedCandidate(
            source_id=f"datg:{'a' * 48}",
            text=" ",
            phonemes=("p",),
        )
    candidate = DatgGeneratedCandidate(source_id=f"datg:{'a' * 48}", text="Peas.", phonemes=("p",))
    base_result = DatgGuidedGenerationResult(
        manifest=manifest,
        candidates=(candidate,),
        attribute_token_ids=(0,),
        anti_attribute_token_ids=(),
        elapsed_seconds=0.1,
    )
    with pytest.raises(ValidationError, match="source IDs"):
        DatgGuidedGenerationResult.model_validate(
            base_result.model_dump() | {"candidates": (candidate, candidate)}
        )
    with pytest.raises(ValidationError, match="guided token IDs"):
        DatgGuidedGenerationResult.model_validate(
            base_result.model_dump() | {"attribute_token_ids": (1, 0)}
        )


def test_logit_delta_preview_enforces_exact_bounded_matrix_contract() -> None:
    internal = DatgLogitPreviewResult(
        original_logits=((0.0, 1.0, 2.0),),
        modified_logits=((-1.25, 3.5, 2.0),),
        attribute_token_ids=(1,),
        anti_attribute_token_ids=(0,),
    )
    result = DatgLogitDeltaPreviewResult.from_preview(
        cache_key_sha256="a" * 64,
        preview=internal,
    )
    assert result.delta_logits == ((-1.25, 2.5, 0.0),)
    for update in (
        {"delta_logits": ((-1.25, 99.0, 0.0),)},
        {"modified_logits": ((-1.25, 3.5),)},
        {"attribute_token_ids": (3,)},
    ):
        with pytest.raises(ValidationError):
            DatgLogitDeltaPreviewResult.model_validate(result.model_dump() | update)

    bounded = {
        "cache_key_sha256": "a" * 64,
        "target_phonemes": ("p",),
        "target_units": ("p",),
    }
    DatgLogitDeltaPreviewRequest.model_validate(
        bounded | {"logits": tuple((0.0,) * 2_048 for _ in range(8))}
    )
    for logits in (
        tuple((0.0,) for _ in range(9)),
        ((0.0,) * 2_049,),
    ):
        with pytest.raises(ValidationError):
            DatgLogitDeltaPreviewRequest.model_validate(bounded | {"logits": logits})


def test_inspection_matches_upstream_target_covered_and_strict_frequency_semantics() -> None:
    value = artifact()
    service = DatgInspectionService(MemoryCache(value))
    target = service.target(
        DatgTargetInspectionRequest(
            cache_key_sha256=value.identity.cache_key_sha256,
            target_units=("b",),
            max_results=1,
        )
    )
    assert target.token_ids == (1,)
    assert target.total_matches == 2
    assert target.truncated is True
    assert target.matches[0].decoded_text == "second"

    covered = service.covered(
        DatgCoveredInspectionRequest(
            cache_key_sha256=value.identity.cache_key_sha256,
            covered_units=("p",),
        )
    )
    assert covered.token_ids == (0,)

    frequency = service.frequency(
        DatgFrequencyInspectionRequest(
            cache_key_sha256=value.identity.cache_key_sha256,
            unit_counts=(
                DatgUnitFrequency(unit="b", count=1),
                DatgUnitFrequency(unit="p", count=2),
            ),
            threshold=1,
        )
    )
    assert frequency.token_ids == (0,)


def test_inspection_rejects_duplicates_wrong_levels_missing_and_broken_cache() -> None:
    value = artifact()
    service = DatgInspectionService(MemoryCache(value))
    with pytest.raises(InvalidRequestError) as duplicate:
        service.target(
            DatgTargetInspectionRequest(
                cache_key_sha256=value.identity.cache_key_sha256,
                target_units=("p", "p"),
            )
        )
    assert duplicate.value.operation == "datg.inspect.units"
    with pytest.raises(InvalidRequestError) as wrong_level:
        service.covered(
            DatgCoveredInspectionRequest(
                cache_key_sha256=value.identity.cache_key_sha256,
                covered_units=("p-b",),
            )
        )
    assert wrong_level.value.operation == "datg.inspect.unit_level"
    with pytest.raises(InvalidRequestError) as missing:
        DatgInspectionService(MemoryCache(None)).target(
            DatgTargetInspectionRequest(
                cache_key_sha256=value.identity.cache_key_sha256,
                target_units=("p",),
            )
        )
    assert missing.value.operation == "datg.index.not_found"
    with pytest.raises(EngineUnavailableError) as unavailable:
        DatgInspectionService(MemoryCache(value, fail=True)).target(
            DatgTargetInspectionRequest(
                cache_key_sha256=value.identity.cache_key_sha256,
                target_units=("p",),
            )
        )
    assert unavailable.value.operation == "datg.index.cache"
    assert "secret" not in str(unavailable.value)

    class ApplicationErrorCache:
        def get(self, cache_key_sha256: str) -> DatgIndexArtifact | None:
            del cache_key_sha256
            raise InvalidRequestError("datg.cache.denied")

    with pytest.raises(InvalidRequestError) as denied:
        DatgInspectionService(ApplicationErrorCache()).target(
            DatgTargetInspectionRequest(
                cache_key_sha256=value.identity.cache_key_sha256,
                target_units=("p",),
            )
        )
    assert denied.value.operation == "datg.cache.denied"

    class WrongCache:
        def get(self, cache_key_sha256: str) -> DatgIndexArtifact | None:
            del cache_key_sha256
            return cast(DatgIndexArtifact, object())

    with pytest.raises(EngineUnavailableError) as wrong_type:
        DatgInspectionService(WrongCache()).target(
            DatgTargetInspectionRequest(
                cache_key_sha256=value.identity.cache_key_sha256,
                target_units=("p",),
            )
        )
    assert wrong_type.value.operation == "datg.index.cache_contract"

    corrupted = value.model_copy(update={"content_sha256": "0" * 64})
    with pytest.raises(EngineUnavailableError) as corrupt_contract:
        DatgInspectionService(MemoryCache(corrupted)).target(
            DatgTargetInspectionRequest(
                cache_key_sha256=value.identity.cache_key_sha256,
                target_units=("p",),
            )
        )
    assert corrupt_contract.value.operation == "datg.index.cache_contract"

    with pytest.raises(EngineUnavailableError) as wrong_key:
        DatgInspectionService(MemoryCache(value)).target(
            DatgTargetInspectionRequest(
                cache_key_sha256="0" * 64,
                target_units=("p",),
            )
        )
    assert wrong_key.value.operation == "datg.index.cache_contract"


def test_policy_validation_and_coordinator_sanitize_engine_failures() -> None:
    value = artifact()
    cpu_policy = DatgRuntimePolicy((policy_entry(),), worker_profile=DatgWorkerProfile.LOCAL_CPU)
    build_request = DatgIndexBuildRequest(runtime_id="tiny-datg", activity_timeout_seconds=9)
    validation = cpu_policy.validate_build(build_request)
    assert validation.required_deployment_profile == "batch-cpu"
    assert validation.worker_only is True
    with pytest.raises(InvalidRequestError):
        cpu_policy.authorize("missing-runtime")
    with pytest.raises(InvalidRequestError) as profile_error:
        cpu_policy.validate_generation(generation_request(value))
    assert profile_error.value.operation == "datg.runtime.worker_profile"

    gpu_policy = DatgRuntimePolicy((policy_entry(),), worker_profile=DatgWorkerProfile.LOCAL_GPU)
    invalid_quantization = generation_request(value).model_copy(
        update={"quantization": DatgQuantization.EIGHT_BIT}
    )
    with pytest.raises(InvalidRequestError) as quantization_error:
        gpu_policy.validate_generation(invalid_quantization)
    assert quantization_error.value.operation == "datg.runtime.quantization"
    assert gpu_policy.validate_generation(
        generation_request(value)
    ).required_deployment_profile == ("gpu-inference")

    engine = RecordingEngine()
    coordinator = DatgCoordinator(cpu_policy, engine, MemoryCache(value))  # type: ignore[arg-type]
    with pytest.raises(EngineUnavailableError) as sanitized:
        coordinator.build_index(build_request)
    assert sanitized.value.operation == "datg.index.build"
    assert "secret" not in str(sanitized.value)


def test_lab_http_has_no_execution_route_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    value = artifact()
    pure_inspection = DatgInspectionService(MemoryCache(value))

    class AuthorizedInspection:
        async def list(
            self,
            actor: DatgCatalogActor,
            *,
            project_id: UUID,
            offset: int,
            limit: int,
        ) -> tuple[DatgIndexPublication, ...]:
            del actor, project_id, offset, limit
            return (
                DatgIndexPublication(
                    build_run_id=UUID("00000000-0000-4000-8000-000000000004"),
                    cache_key_sha256=value.identity.cache_key_sha256,
                    content_sha256=value.content_sha256,
                    runtime_id="tiny-datg",
                    language="en-us",
                    unit=DatgUnit.PHONEME,
                    vocabulary_size=value.vocabulary_size,
                    indexed_token_count=value.indexed_token_count,
                    size_bytes=len(value.model_dump_json().encode()),
                    created_at=datetime.now(UTC),
                ),
            )

        async def target(
            self,
            actor: DatgCatalogActor,
            *,
            project_id: UUID,
            request: DatgTargetInspectionRequest,
        ) -> DatgInspectionResult:
            del actor, project_id
            return pure_inspection.target(request)

        async def covered(
            self,
            actor: DatgCatalogActor,
            *,
            project_id: UUID,
            request: DatgCoveredInspectionRequest,
        ) -> DatgInspectionResult:
            del actor, project_id
            return pure_inspection.covered(request)

        async def frequency(
            self,
            actor: DatgCatalogActor,
            *,
            project_id: UUID,
            request: DatgFrequencyInspectionRequest,
        ) -> DatgInspectionResult:
            del actor, project_id
            return pure_inspection.frequency(request)

        async def preview_logits(
            self,
            actor: DatgCatalogActor,
            *,
            project_id: UUID,
            request: DatgLogitDeltaPreviewRequest,
        ) -> DatgLogitDeltaPreviewResult:
            del actor, project_id
            preview = CorpusgenDatgBindings().preview(
                DatgLogitPreviewRequest(
                    artifact=value,
                    target_phonemes=request.target_phonemes,
                    target_units=request.target_units,
                    coverage_sequences=request.coverage_sequences,
                    guidance=request.guidance,
                    logits=request.logits,
                )
            )
            return DatgLogitDeltaPreviewResult.from_preview(
                cache_key_sha256=value.identity.cache_key_sha256,
                preview=preview,
            )

    inspection = AuthorizedInspection()
    policy = DatgRuntimePolicy((policy_entry(),), worker_profile=DatgWorkerProfile.LOCAL_GPU)
    app = FastAPI()
    app.state.authenticator = DemoAuthenticator()
    app.state.rate_limiter = DisabledRateLimiter()
    app.include_router(datg_lab_router(inspection, policy))

    def forbidden_connect(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("HTTP validation must not open network sockets")

    monkeypatch.setattr(socket, "create_connection", forbidden_connect)
    client = TestClient(app)
    catalog = client.get(f"/projects/{PROJECT_ID}/datg/indexes")
    assert catalog.status_code == 200
    assert catalog.json()[0]["cache_key_sha256"] == value.identity.cache_key_sha256
    response = client.post(
        f"/projects/{PROJECT_ID}/datg/index/inspect/targets",
        json={
            "cache_key_sha256": value.identity.cache_key_sha256,
            "target_units": ["b"],
        },
    )
    assert response.status_code == 200
    assert response.json()["token_ids"] == [1, 2]
    covered = client.post(
        f"/projects/{PROJECT_ID}/datg/index/inspect/anti/covered",
        json={
            "cache_key_sha256": value.identity.cache_key_sha256,
            "covered_units": ["p"],
        },
    )
    assert covered.status_code == 200
    assert covered.json()["token_ids"] == [0]
    frequency = client.post(
        f"/projects/{PROJECT_ID}/datg/index/inspect/anti/frequency",
        json={
            "cache_key_sha256": value.identity.cache_key_sha256,
            "unit_counts": [{"unit": "p", "count": 2}],
            "threshold": 1,
        },
    )
    assert frequency.status_code == 200
    assert frequency.json()["token_ids"] == [0]
    preview = client.post(
        f"/projects/{PROJECT_ID}/datg/index/preview/logits",
        json={
            "cache_key_sha256": value.identity.cache_key_sha256,
            "target_phonemes": ["b", "p"],
            "target_units": ["b"],
            "coverage_sequences": [{"phonemes": ["p"]}],
            "guidance": {
                "boost_strength": 2.5,
                "penalty_strength": -1.25,
                "anti_attribute_mode": "covered",
                "frequency_threshold": 0,
            },
            "logits": [[0.0, 1.0, 2.0]],
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json() == {
        "schema_id": "corpuskit.datg-logit-delta-preview.v1",
        "cache_key_sha256": value.identity.cache_key_sha256,
        "original_logits": [[0.0, 1.0, 2.0]],
        "delta_logits": [[-1.25, 2.5, 2.5]],
        "modified_logits": [[-1.25, 3.5, 4.5]],
        "attribute_token_ids": [1, 2],
        "anti_attribute_token_ids": [0],
        "generation_executed": False,
        "model_loaded": False,
        "network_used": False,
    }
    request_schema = app.openapi()["components"]["schemas"]["DatgLogitDeltaPreviewRequest"]
    assert "artifact" not in request_schema["properties"]
    build_validation = client.post(
        "/datg/index/validate",
        json=DatgIndexBuildRequest(runtime_id="tiny-datg").model_dump(mode="json"),
    )
    assert build_validation.status_code == 200
    assert build_validation.json()["required_deployment_profile"] == "batch-cpu"
    validate = client.post(
        "/datg/generation/validate",
        json=generation_request(value).model_dump(mode="json"),
    )
    assert validate.status_code == 200
    assert validate.json()["worker_only"] is True
    assert client.post("/datg/generation/execute", json={}).status_code == 404
