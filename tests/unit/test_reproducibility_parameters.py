"""Kind-aware manifest metadata extraction for every nested advanced DTO."""

from __future__ import annotations

import pytest

from corpuskit.domain.datg import DatgGuidedGenerationRequest, DatgIndexBuildRequest
from corpuskit.domain.generation import (
    GenerationStoppingCriteria,
    GenerationTarget,
    HuggingFaceRepository,
    HuggingFaceRepositorySpec,
    RepositoryGenerationRequest,
)
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.model_runtime import (
    HostedGenerationRequest,
    HostedModelSelection,
    ImmutableModelPin,
    LocalGenerationRequest,
    LocalModelSelection,
)
from corpuskit.domain.phon_rl import (
    PhonRlDynamicPromptSource,
    PhonRlTrainingParameters,
    PhonRlTrainingRequest,
)
from corpuskit.services.reproducibility import (
    ReproducibilityError,
    _validated_manifest_parameters,
)


def _stopping() -> GenerationStoppingCriteria:
    return GenerationStoppingCriteria(
        max_sentences=1,
        max_iterations=1,
        timeout_seconds=1,
    )


@pytest.mark.parametrize(
    ("kind", "spec", "expected"),
    [
        (
            RunKind.SELECT,
            {
                "candidates": ["A sentence."],
                "language": "fr-fr",
                "unit": "diphone",
                "target": {"mode": "derived", "phonemes": []},
                "options": {"algorithm": "stochastic", "seed": 41},
            },
            ("fr-fr", "derived", "diphone", 41),
        ),
        (
            RunKind.GENERATE_REPOSITORY,
            RepositoryGenerationRequest(
                source=HuggingFaceRepository(
                    spec=HuggingFaceRepositorySpec(
                        dataset="acme/dataset",
                        config="default",
                        split="train",
                        text_column="text",
                        revision="a" * 40,
                        language="de-de",
                        max_samples=10,
                    )
                ),
                target=GenerationTarget(phonemes=("p",), unit="triphone"),
                stopping=_stopping(),
            ).model_dump(mode="json"),
            ("de-de", "explicit", "triphone", None),
        ),
        (
            RunKind.GENERATE_LLM,
            HostedGenerationRequest(
                selection=HostedModelSelection(
                    provider="openai",
                    model="openai/demo-model",
                    connection_id="demo-provider",
                ),
                target=GenerationTarget(phonemes=("p",), unit="diphone"),
                language="es-es",
                stopping=_stopping(),
                prompt_template_id="coverage-v1",
                external_processing_confirmed=True,
            ).model_dump(mode="json"),
            ("es-es", "explicit", "diphone", None),
        ),
        (
            RunKind.GENERATE_LOCAL,
            LocalGenerationRequest(
                selection=LocalModelSelection(
                    pin=ImmutableModelPin(model="acme/model", revision="b" * 40)
                ),
                target=GenerationTarget(phonemes=("p",), unit="triphone"),
                language="it-it",
                stopping=_stopping(),
                seed=99,
            ).model_dump(mode="json"),
            ("it-it", "explicit", "triphone", 99),
        ),
        (
            RunKind.BUILD_DATG_INDEX,
            DatgIndexBuildRequest(
                runtime_id="datg-v1",
                language="nl-nl",
                unit="diphone",
            ).model_dump(mode="json"),
            ("nl-nl", "none", "diphone", None),
        ),
        (
            RunKind.GENERATE_DATG,
            DatgGuidedGenerationRequest(
                runtime_id="datg-v1",
                index_cache_key_sha256="c" * 64,
                language="pt-br",
                unit="diphone",
                target_phonemes=("p",),
                target_units=("p-p",),
                seed=77,
            ).model_dump(mode="json"),
            ("pt-br", "explicit", "diphone", 77),
        ),
        (
            RunKind.TRAIN_PHON_RL,
            PhonRlTrainingRequest(
                runtime_id="rl-v1",
                language="sv-se",
                unit="triphone",
                target_phonemes=("p",),
                prompt_source=PhonRlDynamicPromptSource(
                    strategy_id="missing-units-v1",
                    requested_prompts=1,
                ),
                parameters=PhonRlTrainingParameters(
                    num_steps=1,
                    batch_size=1,
                    max_new_tokens=8,
                    seed=123,
                ),
            ).model_dump(mode="json"),
            ("sv-se", "explicit", "triphone", 123),
        ),
    ],
)
def test_nested_manifest_parameters_are_extracted_from_validated_dtos(
    kind: RunKind,
    spec: dict[str, object],
    expected: tuple[str, str, str, int | None],
) -> None:
    assert _validated_manifest_parameters(kind, spec) == expected


def test_manifest_parameter_extraction_rejects_malformed_or_reserved_specs() -> None:
    with pytest.raises(ReproducibilityError, match="manifest_spec_invalid"):
        _validated_manifest_parameters(RunKind.SELECT, {"options": {"seed": 7}})
    with pytest.raises(ReproducibilityError, match="manifest_run_kind_unsupported"):
        _validated_manifest_parameters(RunKind.EXPORT, {})
