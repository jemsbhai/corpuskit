"""Server-owned durable RunKind to worker-profile routing."""

from __future__ import annotations

from corpuskit.domain.jobs import RunKind

PROFILE_RUN_KINDS: dict[str, frozenset[RunKind]] = {
    "batch-cpu": frozenset(
        {
            RunKind.PHONEMIZE,
            RunKind.EVALUATE,
            RunKind.DISTRIBUTION,
            RunKind.TRAJECTORY,
            RunKind.ERROR_RATES,
            RunKind.SELECT,
            RunKind.BUILD_DATG_INDEX,
        }
    ),
    "external-provider": frozenset({RunKind.GENERATE_REPOSITORY, RunKind.GENERATE_LLM}),
    "gpu-inference": frozenset(
        {
            RunKind.GENERATE_LOCAL,
            RunKind.PERPLEXITY,
            RunKind.GENERATE_DATG,
        }
    ),
    "gpu-training": frozenset({RunKind.TRAIN_PHON_RL}),
}


def task_queue_for_kind(kind: RunKind) -> str:
    """Return the one server-owned queue for a durable kind; never fall back."""

    matches = tuple(profile for profile, kinds in PROFILE_RUN_KINDS.items() if kind in kinds)
    if len(matches) != 1:
        raise ValueError("run kind has no unique worker profile")
    return matches[0]


def durable_task_queue_map() -> dict[RunKind, str]:
    """Return every admitted durable kind suitable for dispatcher construction."""

    return {
        kind: task_queue_for_kind(kind) for kinds in PROFILE_RUN_KINDS.values() for kind in kinds
    }


assert set().union(*PROFILE_RUN_KINDS.values()) == set(RunKind) - {RunKind.EXPORT}
assert sum(len(kinds) for kinds in PROFILE_RUN_KINDS.values()) == len(RunKind) - 1


__all__ = ["PROFILE_RUN_KINDS", "durable_task_queue_map", "task_queue_for_kind"]
