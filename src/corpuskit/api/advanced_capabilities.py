"""Sanitized authenticated catalog for advanced validation and durable runs."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from corpuskit.config import Settings
from corpuskit.domain.datg import DatgQuantization
from corpuskit.domain.model_runtime import (
    MAX_HOSTED_REQUEST_DELAY_SECONDS,
    ModelDevice,
    ModelQuantization,
)
from corpuskit.persistence.datg_cache import read_only_datg_cache_available


class _CatalogModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HostedModelOption(_CatalogModel):
    provider: str
    model: str
    connection_id: str
    max_output_tokens_per_request: int
    request_delay_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=MAX_HOSTED_REQUEST_DELAY_SECONDS,
    )
    prompt_template_ids: tuple[str, ...]


class LocalModelOption(_CatalogModel):
    model: str
    revision: str
    allowed_devices: tuple[ModelDevice, ...]
    allowed_quantizations: tuple[ModelQuantization, ...]
    allow_phon_rl_adapters: bool


class HuggingFaceRepositoryOption(_CatalogModel):
    dataset: str
    config: str
    split: str
    text_column: str
    revision: str
    language: str
    max_samples: int


class DatgRuntimeOption(_CatalogModel):
    runtime_id: str
    allowed_quantizations: tuple[DatgQuantization, ...]


class PhonRlRuntimeOption(_CatalogModel):
    runtime_id: str
    allow_peft: bool
    allow_static_prompts: bool
    allowed_prompt_strategies: tuple[str, ...]


class AdvancedCapabilityCatalog(_CatalogModel):
    schema_id: Literal["corpuskit.advanced-capabilities.v2"] = "corpuskit.advanced-capabilities.v2"
    advanced_operation_routes_validation_only: Literal[True] = True
    durable_run_submission_route: Literal["/api/v1/runs"] = "/api/v1/runs"
    hosted_models: tuple[HostedModelOption, ...]
    huggingface_repositories: tuple[HuggingFaceRepositoryOption, ...]
    local_models: tuple[LocalModelOption, ...]
    datg_runtimes: tuple[DatgRuntimeOption, ...]
    phon_rl_runtimes: tuple[PhonRlRuntimeOption, ...]
    datg_inspection: Literal["configured_read_only", "unavailable"]
    phon_rl_lab: Literal["bounded_optional_dependency"] = "bounded_optional_dependency"


def advanced_capabilities(settings: Settings) -> AdvancedCapabilityCatalog:
    """Project only non-secret policy selectors required to build valid requests."""

    inspection_configured = read_only_datg_cache_available(
        settings.worker_datg_index_cache_root,
        declared_read_only=settings.worker_datg_cache_mount_read_only,
    )
    return AdvancedCapabilityCatalog(
        hosted_models=tuple(
            HostedModelOption(
                provider=item.provider,
                model=item.model,
                connection_id=item.connection_id,
                max_output_tokens_per_request=item.max_output_tokens_per_request,
                request_delay_seconds=item.request_delay_seconds,
                prompt_template_ids=tuple(
                    template.template_id for template in item.prompt_templates
                ),
            )
            for item in settings.worker_hosted_model_policies
        ),
        huggingface_repositories=tuple(
            HuggingFaceRepositoryOption(
                dataset=item.dataset,
                config=item.config,
                split=item.split,
                text_column=item.text_column,
                revision=item.revision,
                language=item.language,
                max_samples=item.max_samples,
            )
            for item in settings.worker_huggingface_repository_policies
        ),
        local_models=tuple(
            LocalModelOption(
                model=item.pin.model,
                revision=item.pin.revision,
                allowed_devices=item.allowed_devices,
                allowed_quantizations=item.allowed_quantizations,
                allow_phon_rl_adapters=item.allow_phon_rl_adapters,
            )
            for item in settings.worker_local_model_policies
        ),
        datg_runtimes=tuple(
            DatgRuntimeOption(
                runtime_id=item.runtime_id,
                allowed_quantizations=item.allowed_quantizations,
            )
            for item in settings.worker_datg_runtime_policies
        ),
        phon_rl_runtimes=tuple(
            PhonRlRuntimeOption(
                runtime_id=item.runtime_id,
                allow_peft=item.allow_peft,
                allow_static_prompts=item.allow_static_prompts,
                allowed_prompt_strategies=item.allowed_prompt_strategies,
            )
            for item in settings.worker_phon_rl_runtime_policies
        ),
        datg_inspection=("configured_read_only" if inspection_configured else "unavailable"),
    )


def advanced_capabilities_router(catalog: AdvancedCapabilityCatalog) -> APIRouter:
    router = APIRouter()

    @router.get("/advanced/capabilities", response_model=AdvancedCapabilityCatalog)
    async def get_advanced_capabilities() -> AdvancedCapabilityCatalog:
        return catalog

    return router


__all__ = [
    "AdvancedCapabilityCatalog",
    "HuggingFaceRepositoryOption",
    "advanced_capabilities",
    "advanced_capabilities_router",
]
