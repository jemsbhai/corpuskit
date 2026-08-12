from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from corpuskit.adapters.corpusgen.datg import (
    CorpusgenDatgAdapter,
    CorpusgenDatgBindings,
    GuidanceOutput,
    IndexMaps,
    OfflineSnapshotResolver,
    RawCandidate,
    SnapshotLocation,
    TransformersTokenizerLoader,
)
from corpuskit.adapters.corpusgen.model_runtime import compute_snapshot_digest
from corpuskit.domain.datg import (
    DatgAntiMode,
    DatgCacheIdentity,
    DatgGuidanceOptions,
    DatgGuidedGenerationRequest,
    DatgIndexArtifact,
    DatgIndexBuildRequest,
    DatgIndexedToken,
    DatgLogitPreviewRequest,
    DatgLogitPreviewResult,
    DatgPhonemeSequence,
    DatgQuantization,
    DatgRuntimePolicyEntry,
    DatgSnapshotPin,
    DatgUnit,
    DatgUnitTokenSet,
    DatgWorkerProfile,
)
from corpuskit.domain.errors import (
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
)


class TinyTokenizer:
    def __init__(
        self,
        vocabulary: dict[str, int] | None = None,
        decoded: dict[int, object] | None = None,
    ) -> None:
        self.vocabulary = {"pea": 0, "bee": 1, "tea": 2} if vocabulary is None else vocabulary
        self.decoded = {0: "pea", 1: "bee", 2: "tea"} if decoded is None else decoded

    def get_vocab(self) -> dict[str, int]:
        return self.vocabulary

    def decode(self, token_id: int, *, skip_special_tokens: bool) -> str:
        assert skip_special_tokens is True
        return self.decoded[token_id]  # type: ignore[return-value]


class StaticResolver:
    def __init__(self, location: SnapshotLocation) -> None:
        self.location = location
        self.pins: list[DatgSnapshotPin] = []

    def resolve(self, pin: DatgSnapshotPin) -> SnapshotLocation:
        self.pins.append(pin)
        return self.location


class StaticLoader:
    def __init__(self, tokenizer: TinyTokenizer) -> None:
        self.tokenizer = tokenizer
        self.calls: list[tuple[SnapshotLocation, DatgSnapshotPin]] = []

    def load(self, location: SnapshotLocation, pin: DatgSnapshotPin) -> TinyTokenizer:
        self.calls.append((location, pin))
        return self.tokenizer


class FakeBindings:
    def __init__(self) -> None:
        self.maps = IndexMaps(
            unit_to_tokens={"p": {0, 2}, "b": {1, 2}, "p-b": {2}},
            token_units={0: {"p"}, 1: {"b"}, 2: {"p", "b", "p-b"}},
        )
        self.output = GuidanceOutput(
            candidates=(
                RawCandidate(text="Peas bloom.", phonemes=("p", "i", "z")),
                RawCandidate(text="Peas bloom.", phonemes=("p", "i", "z")),
                RawCandidate(text="Bees hum.", phonemes=("b", "i", "z")),
            ),
            attribute_token_ids=(2, 1),
            anti_attribute_token_ids=(0,),
        )
        self.build_calls = 0
        self.generate_calls = 0

    def build_index(
        self,
        tokenizer: object,
        *,
        language: str,
        batch_size: int,
    ) -> IndexMaps:
        del tokenizer
        assert language == "en-us"
        assert batch_size == 2
        self.build_calls += 1
        return self.maps

    def generate(
        self,
        *,
        snapshot: Path,
        request: DatgGuidedGenerationRequest,
        artifact: DatgIndexArtifact,
    ) -> GuidanceOutput:
        assert snapshot.is_dir()
        assert request.seed == 17
        assert artifact.identity.language == "en-us"
        self.generate_calls += 1
        return self.output

    def preview(self, request: DatgLogitPreviewRequest) -> DatgLogitPreviewResult:
        del request
        raise RuntimeError("preview secret /path")


def snapshot(tmp_path: Path) -> tuple[SnapshotLocation, str]:
    root = tmp_path / "cache" / "models--acme--tiny"
    value = root / "snapshots" / ("a" * 40)
    value.mkdir(parents=True)
    (value / "config.json").write_text("{}", encoding="utf-8")
    (value / "model.safetensors").write_bytes(b"safe weights")
    location = SnapshotLocation(snapshot=value, approved_cache_root=root)
    return location, compute_snapshot_digest(value, approved_cache_root=root)


def runtime_policy(digest: str) -> DatgRuntimePolicyEntry:
    pin = DatgSnapshotPin(
        repository_id="acme/tiny",
        revision="a" * 40,
        snapshot_sha256=digest,
    )
    return DatgRuntimePolicyEntry(
        runtime_id="tiny-datg",
        model=pin,
        tokenizer=pin,
        allowed_quantizations=(DatgQuantization.NONE, DatgQuantization.FOUR_BIT),
    )


def build_request(unit: DatgUnit = DatgUnit.PHONEME) -> DatgIndexBuildRequest:
    return DatgIndexBuildRequest(
        runtime_id="tiny-datg",
        unit=unit,
        batch_size=2,
        max_vocabulary_size=10,
        activity_timeout_seconds=10,
    )


def adapter(
    location: SnapshotLocation,
    *,
    tokenizer: TinyTokenizer | None = None,
    bindings: FakeBindings | None = None,
    clock: object | None = None,
) -> CorpusgenDatgAdapter:
    kwargs: dict[str, object] = {}
    if clock is not None:
        kwargs["clock"] = clock
    return CorpusgenDatgAdapter(
        snapshot_resolver=StaticResolver(location),
        tokenizer_loader=StaticLoader(tokenizer or TinyTokenizer()),
        bindings=bindings or FakeBindings(),
        corpusgen_version="0.1.7",
        espeak_version="1.52.0",
        **kwargs,  # type: ignore[arg-type]
    )


def guidance_request(value: DatgIndexArtifact) -> DatgGuidedGenerationRequest:
    return DatgGuidedGenerationRequest(
        runtime_id="tiny-datg",
        index_cache_key_sha256=value.identity.cache_key_sha256,
        target_phonemes=("p", "b"),
        target_units=("b",),
        coverage_sequences=(DatgPhonemeSequence(phonemes=("p",)),),
        quantization=DatgQuantization.FOUR_BIT,
        candidates=3,
        max_new_tokens=32,
        seed=17,
        activity_timeout_seconds=10,
    )


def test_build_index_binds_verified_snapshot_versions_and_public_maps(tmp_path: Path) -> None:
    location, digest = snapshot(tmp_path)
    bindings = FakeBindings()
    engine = adapter(location, bindings=bindings, clock=iter((5.0, 5.25)).__next__)
    result = engine.build_index(build_request(), runtime_policy(digest))
    assert result.elapsed_seconds == 0.25
    assert result.artifact.identity.tokenizer_snapshot_sha256 == digest
    assert result.artifact.identity.corpusgen_version == "0.1.7"
    assert result.artifact.identity.espeak_version == "1.52.0"
    assert result.artifact.vocabulary_size == 3
    assert result.artifact.indexed_token_count == 3
    assert result.artifact.unit_to_tokens[0].unit == "b"
    assert bindings.build_calls == 1


def test_generate_is_profile_gated_deduplicated_and_manifested(tmp_path: Path) -> None:
    location, digest = snapshot(tmp_path)
    bindings = FakeBindings()
    engine = adapter(location, bindings=bindings, clock=iter((4.0, 4.5)).__next__)
    policy = runtime_policy(digest)
    artifact = adapter(location).build_index(build_request(), policy).artifact
    request = guidance_request(artifact)
    result = engine.generate(request, policy, DatgWorkerProfile.LOCAL_GPU, artifact)
    assert [candidate.text for candidate in result.candidates] == ["Peas bloom.", "Bees hum."]
    assert len({candidate.source_id for candidate in result.candidates}) == 2
    assert result.attribute_token_ids == (1, 2)
    assert result.anti_attribute_token_ids == (0,)
    assert result.manifest.seed == 17
    assert result.manifest.local_files_only is True
    assert result.manifest.trust_remote_code is False
    assert result.manifest.safetensors_only is True
    assert result.manifest.reproducibility == "best_effort"
    assert bindings.generate_calls == 1


@pytest.mark.parametrize(
    ("mutator", "operation"),
    [
        (lambda request: request.model_copy(update={"runtime_id": "other-runtime"}), "policy"),
        (
            lambda request: request.model_copy(update={"quantization": DatgQuantization.EIGHT_BIT}),
            "quantization",
        ),
    ],
)
def test_generation_rejects_policy_mismatches(
    tmp_path: Path,
    mutator: object,
    operation: str,
) -> None:
    location, digest = snapshot(tmp_path)
    policy = runtime_policy(digest)
    value = adapter(location).build_index(build_request(), policy).artifact
    request = mutator(guidance_request(value))  # type: ignore[operator]
    with pytest.raises(InvalidRequestError) as error:
        adapter(location).generate(request, policy, DatgWorkerProfile.LOCAL_GPU, value)
    assert error.value.operation == f"datg.runtime.{operation}"


def test_generation_rejects_wrong_profile_identity_empty_and_bad_token_ids(
    tmp_path: Path,
) -> None:
    location, digest = snapshot(tmp_path)
    policy = runtime_policy(digest)
    value = adapter(location).build_index(build_request(), policy).artifact
    request = guidance_request(value)
    with pytest.raises(InvalidRequestError) as profile_error:
        adapter(location).generate(request, policy, DatgWorkerProfile.LOCAL_CPU, value)
    assert profile_error.value.operation == "datg.runtime.worker_profile"

    other = value.model_copy(
        update={
            "identity": DatgCacheIdentity.create(
                tokenizer=policy.tokenizer,
                language="fr-fr",
                unit=DatgUnit.PHONEME,
                corpusgen_version="0.1.7",
                espeak_version="1.52.0",
            )
        }
    )
    with pytest.raises(InvalidRequestError) as identity_error:
        adapter(location).generate(request, policy, DatgWorkerProfile.LOCAL_GPU, other)
    assert identity_error.value.operation == "datg.index.identity"

    empty = FakeBindings()
    empty.output = GuidanceOutput(
        candidates=(), attribute_token_ids=(), anti_attribute_token_ids=()
    )
    with pytest.raises(EngineContractError) as empty_error:
        adapter(location, bindings=empty).generate(
            request, policy, DatgWorkerProfile.LOCAL_GPU, value
        )
    assert empty_error.value.operation == "datg.generation.empty"

    bad_ids = FakeBindings()
    bad_ids.output = GuidanceOutput(
        candidates=(RawCandidate(text="Valid", phonemes=("v",)),),
        attribute_token_ids=(999,),
        anti_attribute_token_ids=(),
    )
    with pytest.raises(EngineContractError) as token_error:
        adapter(location, bindings=bad_ids).generate(
            request, policy, DatgWorkerProfile.LOCAL_GPU, value
        )
    assert token_error.value.operation == "datg.generation.token_ids"

    too_many = FakeBindings()
    too_many.output = GuidanceOutput(
        candidates=(
            RawCandidate(text="One", phonemes=("w",)),
            RawCandidate(text="Two", phonemes=("t",)),
        ),
        attribute_token_ids=(),
        anti_attribute_token_ids=(),
    )
    one_candidate = request.model_copy(update={"candidates": 1})
    with pytest.raises(EngineContractError) as count_error:
        adapter(location, bindings=too_many).generate(
            one_candidate, policy, DatgWorkerProfile.LOCAL_GPU, value
        )
    assert count_error.value.operation == "datg.generation.candidate_count"


@pytest.mark.parametrize(
    "tokenizer",
    [
        TinyTokenizer(vocabulary={}),
        TinyTokenizer(vocabulary={"a": 0, "b": 0}),
        TinyTokenizer(vocabulary={"a": -1}),
        TinyTokenizer(vocabulary={"a": True}),
    ],
)
def test_build_rejects_invalid_or_oversized_vocabularies(
    tmp_path: Path, tokenizer: TinyTokenizer
) -> None:
    location, digest = snapshot(tmp_path)
    with pytest.raises(InvalidRequestError) as error:
        adapter(location, tokenizer=tokenizer).build_index(build_request(), runtime_policy(digest))
    assert error.value.operation == "datg.index.vocabulary"

    oversized = TinyTokenizer(vocabulary={"a": 0, "b": 1})
    request = build_request().model_copy(update={"max_vocabulary_size": 1})
    with pytest.raises(InvalidRequestError):
        adapter(location, tokenizer=oversized).build_index(request, runtime_policy(digest))


def test_build_rejects_adapter_contract_deadline_policy_and_digest_errors(tmp_path: Path) -> None:
    location, digest = snapshot(tmp_path)
    policy = runtime_policy(digest)
    wrong_policy = policy.model_copy(update={"runtime_id": "wrong"})
    with pytest.raises(InvalidRequestError) as policy_error:
        adapter(location).build_index(build_request(), wrong_policy)
    assert policy_error.value.operation == "datg.runtime.policy"

    with pytest.raises(EngineUnavailableError) as digest_error:
        adapter(location).build_index(build_request(), runtime_policy("0" * 64))
    assert digest_error.value.operation == "datg.snapshot.digest"

    inconsistent = FakeBindings()
    inconsistent.maps = IndexMaps(
        unit_to_tokens={"p": {0}},
        token_units={0: {"b"}},
    )
    with pytest.raises(EngineContractError) as mapping_error:
        adapter(location, bindings=inconsistent).build_index(build_request(), policy)
    assert mapping_error.value.operation == "datg.index.mapping"

    external = FakeBindings()
    external.maps = IndexMaps(unit_to_tokens={"p": {99}}, token_units={99: {"p"}})
    with pytest.raises(EngineContractError) as token_error:
        adapter(location, bindings=external).build_index(build_request(), policy)
    assert token_error.value.operation == "datg.index.token_id"

    with pytest.raises(EngineUnavailableError) as deadline_error:
        adapter(location, clock=iter((5.0, 16.0)).__next__).build_index(build_request(), policy)
    assert deadline_error.value.operation == "datg.activity.deadline"


def test_snapshot_tampering_after_policy_pin_is_detected(tmp_path: Path) -> None:
    location, digest = snapshot(tmp_path)
    policy = runtime_policy(digest)
    (location.snapshot / "config.json").write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(EngineUnavailableError) as error:
        adapter(location).build_index(build_request(), policy)
    assert error.value.operation == "datg.snapshot.digest"


def preview_artifact(unit: DatgUnit) -> tuple[DatgIndexArtifact, str, str, tuple[str, ...]]:
    target, covered, target_phonemes = {
        DatgUnit.PHONEME: ("b", "p", ("p", "b")),
        DatgUnit.DIPHONE: ("b-t", "p-b", ("p", "b", "t")),
        DatgUnit.TRIPHONE: ("b-t-k", "p-b-t", ("p", "b", "t", "k")),
    }[unit]
    identity = DatgCacheIdentity.create(
        tokenizer=DatgSnapshotPin(
            repository_id="acme/tiny",
            revision="a" * 40,
            snapshot_sha256="b" * 64,
        ),
        language="en-us",
        unit=unit,
        corpusgen_version="0.1.7",
        espeak_version="1.52.0",
    )
    value = DatgIndexArtifact.create(
        identity=identity,
        vocabulary_size=4,
        unit_to_tokens=(
            DatgUnitTokenSet(unit=target, token_ids=(1, 2)),
            DatgUnitTokenSet(unit=covered, token_ids=(0,)),
        ),
        token_units=(
            DatgIndexedToken(token_id=0, decoded_text="covered", units=(covered,)),
            DatgIndexedToken(token_id=1, decoded_text="target", units=(target,)),
            DatgIndexedToken(token_id=2, decoded_text="target2", units=(target,)),
        ),
    )
    return value, target, covered, target_phonemes


@pytest.mark.parametrize("unit", tuple(DatgUnit))
@pytest.mark.parametrize("anti_mode", tuple(DatgAntiMode))
def test_public_datg_strategy_has_hand_computed_exact_deltas_and_clones(
    unit: DatgUnit,
    anti_mode: DatgAntiMode,
) -> None:
    value, target, _covered, target_phonemes = preview_artifact(unit)
    request = DatgLogitPreviewRequest(
        artifact=value,
        target_phonemes=target_phonemes,
        target_units=(target,),
        coverage_sequences=(DatgPhonemeSequence(phonemes=target_phonemes[:-1]),),
        guidance=DatgGuidanceOptions(
            boost_strength=2.5,
            penalty_strength=-1.25,
            anti_attribute_mode=anti_mode,
            frequency_threshold=0,
        ),
        logits=((0.0, 1.0, 2.0, 3.0), (10.0, 11.0, 12.0, 13.0)),
    )
    result = CorpusgenDatgBindings().preview(request)
    assert result.original_logits == request.logits
    assert request.logits == ((0.0, 1.0, 2.0, 3.0), (10.0, 11.0, 12.0, 13.0))
    assert result.modified_logits == (
        (-1.25, 3.5, 4.5, 3.0),
        (8.75, 13.5, 14.5, 13.0),
    )
    assert result.attribute_token_ids == (1, 2)
    assert result.anti_attribute_token_ids == (0,)


def test_logit_validation_and_sanitized_preview_failure(tmp_path: Path) -> None:
    from corpusgen.generate.phon_datg.modulator import LogitModulator

    value, target, _covered, target_phonemes = preview_artifact(DatgUnit.PHONEME)
    base = {
        "artifact": value,
        "target_phonemes": target_phonemes,
        "target_units": (target,),
        "logits": ((0.0, 1.0),),
    }
    with pytest.raises(ValidationError):
        DatgLogitPreviewRequest.model_validate({**base, "logits": ((0.0,), (0.0, 1.0))})
    with pytest.raises(ValidationError):
        DatgGuidanceOptions(boost_strength=-0.1)
    with pytest.raises(ValidationError):
        DatgGuidanceOptions(penalty_strength=0.1)
    with pytest.raises(ValueError, match="boost_strength"):
        LogitModulator(boost_strength=-0.1)
    with pytest.raises(ValueError, match="penalty_strength"):
        LogitModulator(penalty_strength=0.1)

    location, _digest = snapshot(tmp_path)
    with pytest.raises(EngineUnavailableError) as error:
        adapter(location).preview_logits(DatgLogitPreviewRequest.model_validate(base))
    assert error.value.operation == "datg.logits.preview"
    assert "/path" not in str(error.value)


def test_default_offline_resolver_and_tokenizer_loader_use_fail_closed_kwargs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    revision = "a" * 40
    snapshot_path = tmp_path / "models--acme--tiny" / "snapshots" / revision
    snapshot_path.mkdir(parents=True)
    download_calls: list[dict[str, object]] = []

    def snapshot_download(**kwargs: object) -> str:
        download_calls.append(kwargs)
        return str(snapshot_path)

    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download)
    )
    pin = DatgSnapshotPin(
        repository_id="acme/tiny",
        revision=revision,
        snapshot_sha256="b" * 64,
    )
    location = OfflineSnapshotResolver().resolve(pin)
    assert download_calls == [
        {"repo_id": "acme/tiny", "revision": revision, "local_files_only": True}
    ]
    assert location.approved_cache_root == snapshot_path.parent.parent

    configured = OfflineSnapshotResolver(tmp_path).resolve(pin)
    assert configured.approved_cache_root == tmp_path.resolve()
    assert download_calls[-1] == {
        "repo_id": "acme/tiny",
        "revision": revision,
        "local_files_only": True,
        "cache_dir": str(tmp_path.absolute()),
    }

    load_calls: list[tuple[str, dict[str, object]]] = []

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(path: str, **kwargs: object) -> TinyTokenizer:
            load_calls.append((path, kwargs))
            return TinyTokenizer()

    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoTokenizer=AutoTokenizer))
    loaded = TransformersTokenizerLoader().load(location, pin)
    assert loaded.get_vocab() == TinyTokenizer().get_vocab()
    assert load_calls[0][1] == {
        "revision": revision,
        "local_files_only": True,
        "trust_remote_code": False,
    }


def test_default_resolver_and_loader_sanitize_missing_bad_layout_and_load_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pin = DatgSnapshotPin(
        repository_id="acme/tiny",
        revision="a" * 40,
        snapshot_sha256="b" * 64,
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    with pytest.raises(DependencyUnavailableError) as dependency:
        OfflineSnapshotResolver().resolve(pin)
    assert dependency.value.operation == "datg.snapshot.dependency"

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=lambda **kwargs: str(tmp_path / "wrong-layout")),
    )
    with pytest.raises(EngineUnavailableError) as layout:
        OfflineSnapshotResolver().resolve(pin)
    assert layout.value.operation == "datg.snapshot.layout"

    def fail_download(**kwargs: object) -> str:
        del kwargs
        raise RuntimeError("private hub error")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=fail_download),
    )
    with pytest.raises(EngineUnavailableError) as resolution:
        OfflineSnapshotResolver().resolve(pin)
    assert resolution.value.operation == "datg.snapshot.resolve"

    location = SnapshotLocation(snapshot=tmp_path / "absent", approved_cache_root=tmp_path)
    monkeypatch.setitem(sys.modules, "transformers", None)
    with pytest.raises(DependencyUnavailableError) as tokenizer_dependency:
        TransformersTokenizerLoader().load(location, pin)
    assert tokenizer_dependency.value.operation == "datg.tokenizer.dependency"

    class BrokenAutoTokenizer:
        @staticmethod
        def from_pretrained(path: str, **kwargs: object) -> object:
            del path, kwargs
            raise RuntimeError("C:/private/tokenizer")

    location.snapshot.mkdir()
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(AutoTokenizer=BrokenAutoTokenizer),
    )
    with pytest.raises(EngineUnavailableError) as load:
        TransformersTokenizerLoader().load(location, pin)
    assert load.value.operation == "datg.tokenizer.load"


@pytest.mark.parametrize("quantization", [DatgQuantization.NONE, DatgQuantization.FOUR_BIT])
@pytest.mark.parametrize("anti_mode", [DatgAntiMode.COVERED, DatgAntiMode.FREQUENCY])
def test_default_generation_binding_uses_public_upstream_contract_and_safe_loader_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    quantization: DatgQuantization,
    anti_mode: DatgAntiMode,
) -> None:
    import corpusgen.generate.backends.local as backend_module
    import corpusgen.generate.phon_ctg.targets as target_module
    import corpusgen.generate.phon_datg.graph as graph_module

    value, target, covered, target_phonemes = preview_artifact(DatgUnit.PHONEME)
    seeds: list[int] = []
    calls: list[dict[str, object]] = []

    class FakeTargets:
        unit = "phoneme"

        def __init__(self, *, target_phonemes: list[str], unit: str) -> None:
            assert target_phonemes
            self.unit = unit
            self.covered_units: set[str] = set()
            self.tracker = SimpleNamespace(phoneme_counts={})

        def update(self, phonemes: list[str], sentence_index: int) -> None:
            assert sentence_index == 0
            self.covered_units.update(phonemes)
            self.tracker.phoneme_counts = dict.fromkeys(phonemes, 1)

    class FakeStrategy:
        def __init__(self, **kwargs: object) -> None:
            self.targets: Any = kwargs["targets"]
            self.index: Any = kwargs["attribute_word_index"]
            self.mode = kwargs["anti_attribute_mode"]
            self.threshold = kwargs["frequency_threshold"]
            self.current_attribute_tokens: set[int] = set()
            self.current_anti_attribute_tokens: set[int] = set()

        def prepare(self, target_units: list[str], model: object, tokenizer: object) -> None:
            del model, tokenizer
            self.index.build(None)
            assert self.index.is_built is True
            self.current_attribute_tokens = self.index.get_attribute_tokens(target_units)
            if self.mode == "covered":
                self.current_anti_attribute_tokens = self.index.get_anti_attribute_tokens(
                    self.targets.covered_units, unit_level=self.targets.unit
                )
            else:
                self.current_anti_attribute_tokens = (
                    self.index.get_anti_attribute_tokens_by_frequency(
                        self.targets.tracker.phoneme_counts,
                        self.threshold,
                        unit_level=self.targets.unit,
                    )
                )

    class FakeBackend:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)
            self.strategy: Any = kwargs["guidance_strategy"]

        def generate(self, target_units: list[str], *, k: int) -> list[dict[str, object]]:
            assert k == 1
            self.strategy.prepare(target_units, None, None)
            return [{"text": " Guided pea. ", "phonemes": ["p", "i"]}]

    monkeypatch.setattr(target_module, "PhoneticTargetInventory", FakeTargets)
    monkeypatch.setattr(graph_module, "DATGStrategy", FakeStrategy)
    monkeypatch.setattr(backend_module, "LocalBackend", FakeBackend)
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(set_seed=lambda seed: seeds.append(seed)),
    )
    request = DatgGuidedGenerationRequest(
        runtime_id="tiny-datg",
        index_cache_key_sha256=value.identity.cache_key_sha256,
        target_phonemes=target_phonemes,
        target_units=(target,),
        coverage_sequences=(DatgPhonemeSequence(phonemes=(covered,)),),
        guidance=DatgGuidanceOptions(
            anti_attribute_mode=anti_mode,
            frequency_threshold=0,
        ),
        quantization=quantization,
        candidates=1,
        max_new_tokens=9,
        seed=41,
    )
    output = CorpusgenDatgBindings().generate(
        snapshot=tmp_path,
        request=request,
        artifact=value,
    )
    assert output.candidates == (RawCandidate(text="Guided pea.", phonemes=("p", "i")),)
    assert output.attribute_token_ids == (1, 2)
    assert output.anti_attribute_token_ids == (0,)
    assert seeds == [41]
    assert calls[0]["quantization"] == (None if quantization is DatgQuantization.NONE else "4bit")
    assert calls[0]["model_kwargs"] == {
        "revision": "a" * 40,
        "local_files_only": True,
        "trust_remote_code": False,
        "use_safetensors": True,
    }


def test_adapter_default_versions_and_application_error_preview_passthrough(
    tmp_path: Path,
) -> None:
    # Construction proves the installed base runtime exposes real CorpusGen/eSpeak versions.
    CorpusgenDatgAdapter()
    location, _digest = snapshot(tmp_path)

    class ContractBindings(FakeBindings):
        def preview(self, request: DatgLogitPreviewRequest) -> DatgLogitPreviewResult:
            del request
            raise EngineContractError("datg.test.contract")

    value, target, _covered, target_phonemes = preview_artifact(DatgUnit.PHONEME)
    request = DatgLogitPreviewRequest(
        artifact=value,
        target_phonemes=target_phonemes,
        target_units=(target,),
        logits=((0.0, 0.0, 0.0),),
    )
    with pytest.raises(EngineContractError) as error:
        adapter(location, bindings=ContractBindings()).preview_logits(request)
    assert error.value.operation == "datg.test.contract"


@pytest.mark.integration
def test_real_espeak_attribute_index_acceptance() -> None:
    pytest.importorskip("phonemizer")
    maps = CorpusgenDatgBindings().build_index(
        TinyTokenizer(
            vocabulary={"pea": 0, "bee": 1, "tea": 2},
            decoded={0: "pea", 1: "bee", 2: "tea"},
        ),
        language="en-us",
        batch_size=2,
    )
    assert maps.token_units
    assert set(maps.token_units) == {0, 1, 2}
    assert any("p" in units for units in maps.token_units.values())
