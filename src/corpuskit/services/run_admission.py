"""Pure, fail-closed admission policy for advanced durable run specifications."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from corpuskit.config import Settings
from corpuskit.domain.datg import (
    DatgGuidedGenerationRequest,
    DatgIndexBuildRequest,
    DatgWorkerProfile,
)
from corpuskit.domain.errors import InvalidRequestError
from corpuskit.domain.generation import (
    GenerationExecutionMode,
    HuggingFaceRepositorySpec,
    RepositoryGenerationRequest,
)
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.model_runtime import (
    HostedGenerationRequest,
    LanguageModelAnalysisRequest,
    LocalGenerationRequest,
    WorkerModelProfile,
)
from corpuskit.domain.phon_rl import PhonRlTrainingRequest, PhonRlWorkerProfile
from corpuskit.services.datg import DatgRuntimePolicy
from corpuskit.services.generation_scoring import validate_repository_request
from corpuskit.services.model_runtime import ModelRuntimePolicy
from corpuskit.services.phon_rl import PhonRlRuntimePolicy

ADVANCED_RUN_KINDS = frozenset(
    {
        RunKind.GENERATE_LLM,
        RunKind.GENERATE_REPOSITORY,
        RunKind.GENERATE_LOCAL,
        RunKind.PERPLEXITY,
        RunKind.BUILD_DATG_INDEX,
        RunKind.GENERATE_DATG,
        RunKind.TRAIN_PHON_RL,
    }
)
UNSUPPORTED_RUN_KINDS = frozenset({RunKind.EXPORT})


class RunAdmissionPolicy(Protocol):
    """Authorize a normalized immutable run spec without executing work."""

    def validate(self, kind: RunKind, spec: Mapping[str, Any]) -> None: ...


class DenyAdvancedRunAdmission:
    """Secure default for services constructed without an operator policy."""

    def validate(self, kind: RunKind, spec: Mapping[str, Any]) -> None:
        del spec
        if kind in UNSUPPORTED_RUN_KINDS:
            raise InvalidRequestError("run.kind.unsupported")
        if kind in ADVANCED_RUN_KINDS:
            raise InvalidRequestError("run.advanced.allowlist")


class ConfiguredRunAdmission:
    """Parse exact worker DTOs and apply matching server-owned allowlists."""

    def __init__(
        self,
        *,
        model_runtime: ModelRuntimePolicy,
        datg: DatgRuntimePolicy,
        phon_rl: PhonRlRuntimePolicy,
        huggingface_repositories: tuple[HuggingFaceRepositorySpec, ...] = (),
    ) -> None:
        self.model_runtime = model_runtime
        self.datg = datg
        self.phon_rl = phon_rl
        self.huggingface_repositories = huggingface_repositories

    @classmethod
    def from_settings(cls, settings: Settings) -> ConfiguredRunAdmission:
        """Build a no-I/O policy from immutable process configuration."""

        return cls(
            model_runtime=ModelRuntimePolicy(
                hosted_models=settings.worker_hosted_model_policies,
                local_models=settings.worker_local_model_policies,
                worker_profile=WorkerModelProfile.LOCAL_GPU,
            ),
            datg=DatgRuntimePolicy(
                settings.worker_datg_runtime_policies,
                worker_profile=DatgWorkerProfile.LOCAL_GPU,
            ),
            phon_rl=PhonRlRuntimePolicy(
                settings.worker_phon_rl_runtime_policies,
                worker_profile=PhonRlWorkerProfile.LOCAL_GPU,
            ),
            huggingface_repositories=settings.worker_huggingface_repository_policies,
        )

    def validate(self, kind: RunKind, spec: Mapping[str, Any]) -> None:
        """Reject malformed or unauthorized advanced requests before persistence."""

        if kind in UNSUPPORTED_RUN_KINDS:
            raise InvalidRequestError("run.kind.unsupported")
        if kind is RunKind.GENERATE_REPOSITORY:
            validate_repository_request(
                RepositoryGenerationRequest.model_validate(spec),
                execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
                allowed_huggingface_sources=self.huggingface_repositories,
            )
        elif kind is RunKind.GENERATE_LLM:
            self.model_runtime.validate_hosted(HostedGenerationRequest.model_validate(spec))
        elif kind is RunKind.GENERATE_LOCAL:
            self.model_runtime.validate_local(LocalGenerationRequest.model_validate(spec))
        elif kind is RunKind.PERPLEXITY:
            self.model_runtime.validate_analysis(LanguageModelAnalysisRequest.model_validate(spec))
        elif kind is RunKind.BUILD_DATG_INDEX:
            self.datg.validate_build(DatgIndexBuildRequest.model_validate(spec))
        elif kind is RunKind.GENERATE_DATG:
            self.datg.validate_generation(DatgGuidedGenerationRequest.model_validate(spec))
        elif kind is RunKind.TRAIN_PHON_RL:
            self.phon_rl.validate(PhonRlTrainingRequest.model_validate(spec))


__all__ = [
    "ADVANCED_RUN_KINDS",
    "UNSUPPORTED_RUN_KINDS",
    "ConfiguredRunAdmission",
    "DenyAdvancedRunAdmission",
    "RunAdmissionPolicy",
]
