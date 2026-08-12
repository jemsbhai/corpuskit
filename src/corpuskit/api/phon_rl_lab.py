"""Bounded Phon-RL reward/PPO lab and training control-plane routes."""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import APIRouter, Body
from fastapi.params import Depends as DependsParameter
from starlette.concurrency import run_in_threadpool

from corpuskit.domain.phon_rl import (
    PhonRlClipLossRequest,
    PhonRlGaeRequest,
    PhonRlGaeResult,
    PhonRlHierarchicalRewardRequest,
    PhonRlHierarchicalRewardResult,
    PhonRlKlRequest,
    PhonRlLogProbRequest,
    PhonRlMatrixResult,
    PhonRlResourceEstimate,
    PhonRlScalarResult,
    PhonRlSentenceRewardRequest,
    PhonRlSentenceRewardResult,
    PhonRlTokenRewardRequest,
    PhonRlTokenRewardResult,
    PhonRlTrainingRequest,
    PhonRlTrainingValidationResult,
    PhonRlValueHeadRequest,
    PhonRlValueHeadResult,
)


class PhonRlLabHttpService(Protocol):
    def peek(self, request: PhonRlSentenceRewardRequest) -> PhonRlSentenceRewardResult: ...

    def commit(self, request: PhonRlSentenceRewardRequest) -> PhonRlSentenceRewardResult: ...

    def token_rewards(self, request: PhonRlTokenRewardRequest) -> PhonRlTokenRewardResult: ...

    def hierarchical(
        self,
        request: PhonRlHierarchicalRewardRequest,
    ) -> PhonRlHierarchicalRewardResult: ...

    def log_probs(self, request: PhonRlLogProbRequest) -> PhonRlMatrixResult: ...

    def kl_penalty(self, request: PhonRlKlRequest) -> PhonRlMatrixResult: ...

    def gae(self, request: PhonRlGaeRequest) -> PhonRlGaeResult: ...

    def clip_loss(self, request: PhonRlClipLossRequest) -> PhonRlScalarResult: ...

    def value_head(self, request: PhonRlValueHeadRequest) -> PhonRlValueHeadResult: ...


class PhonRlTrainingHttpPolicy(Protocol):
    def validate(self, request: PhonRlTrainingRequest) -> PhonRlTrainingValidationResult: ...

    def estimate(self, request: PhonRlTrainingRequest) -> PhonRlResourceEstimate: ...


def phon_rl_lab_router(
    lab: PhonRlLabHttpService,
    policy: PhonRlTrainingHttpPolicy,
    *,
    lab_dependencies: tuple[DependsParameter, ...] = (),
    validation_dependencies: tuple[DependsParameter, ...] = (),
) -> APIRouter:
    """Expose CPU-only calculations and policy checks; no training route exists."""

    router = APIRouter()

    @router.post(
        "/phon-rl/reward/peek",
        response_model=PhonRlSentenceRewardResult,
        dependencies=lab_dependencies,
    )
    async def reward_peek(
        request: Annotated[PhonRlSentenceRewardRequest, Body()],
    ) -> PhonRlSentenceRewardResult:
        return await run_in_threadpool(lab.peek, request)

    @router.post(
        "/phon-rl/reward/commit",
        response_model=PhonRlSentenceRewardResult,
        dependencies=lab_dependencies,
    )
    async def reward_commit(
        request: Annotated[PhonRlSentenceRewardRequest, Body()],
    ) -> PhonRlSentenceRewardResult:
        return await run_in_threadpool(lab.commit, request)

    @router.post(
        "/phon-rl/reward/tokens",
        response_model=PhonRlTokenRewardResult,
        dependencies=lab_dependencies,
    )
    async def token_rewards(
        request: Annotated[PhonRlTokenRewardRequest, Body()],
    ) -> PhonRlTokenRewardResult:
        return await run_in_threadpool(lab.token_rewards, request)

    @router.post(
        "/phon-rl/reward/hierarchical",
        response_model=PhonRlHierarchicalRewardResult,
        dependencies=lab_dependencies,
    )
    async def hierarchical_reward(
        request: Annotated[PhonRlHierarchicalRewardRequest, Body()],
    ) -> PhonRlHierarchicalRewardResult:
        return await run_in_threadpool(lab.hierarchical, request)

    @router.post(
        "/phon-rl/ppo/log-probabilities",
        response_model=PhonRlMatrixResult,
        dependencies=lab_dependencies,
    )
    async def log_probabilities(
        request: Annotated[PhonRlLogProbRequest, Body()],
    ) -> PhonRlMatrixResult:
        return await run_in_threadpool(lab.log_probs, request)

    @router.post(
        "/phon-rl/ppo/kl-penalty",
        response_model=PhonRlMatrixResult,
        dependencies=lab_dependencies,
    )
    async def kl_penalty(
        request: Annotated[PhonRlKlRequest, Body()],
    ) -> PhonRlMatrixResult:
        return await run_in_threadpool(lab.kl_penalty, request)

    @router.post(
        "/phon-rl/ppo/gae",
        response_model=PhonRlGaeResult,
        dependencies=lab_dependencies,
    )
    async def gae(request: Annotated[PhonRlGaeRequest, Body()]) -> PhonRlGaeResult:
        return await run_in_threadpool(lab.gae, request)

    @router.post(
        "/phon-rl/ppo/clip-loss",
        response_model=PhonRlScalarResult,
        dependencies=lab_dependencies,
    )
    async def clip_loss(
        request: Annotated[PhonRlClipLossRequest, Body()],
    ) -> PhonRlScalarResult:
        return await run_in_threadpool(lab.clip_loss, request)

    @router.post(
        "/phon-rl/ppo/value-head",
        response_model=PhonRlValueHeadResult,
        dependencies=lab_dependencies,
    )
    async def value_head(
        request: Annotated[PhonRlValueHeadRequest, Body()],
    ) -> PhonRlValueHeadResult:
        return await run_in_threadpool(lab.value_head, request)

    @router.post(
        "/phon-rl/training/validate",
        response_model=PhonRlTrainingValidationResult,
        dependencies=validation_dependencies,
    )
    async def validate_training(
        request: Annotated[PhonRlTrainingRequest, Body()],
    ) -> PhonRlTrainingValidationResult:
        return policy.validate(request)

    @router.post(
        "/phon-rl/training/estimate",
        response_model=PhonRlResourceEstimate,
        dependencies=validation_dependencies,
    )
    async def estimate_training(
        request: Annotated[PhonRlTrainingRequest, Body()],
    ) -> PhonRlResourceEstimate:
        return policy.estimate(request)

    return router


__all__ = ["PhonRlLabHttpService", "PhonRlTrainingHttpPolicy", "phon_rl_lab_router"]
