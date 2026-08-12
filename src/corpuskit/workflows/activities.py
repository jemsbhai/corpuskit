"""Temporal activities that reload specs and atomically project run lifecycle state."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from corpuskit.domain.errors import (
    ApplicationError as DomainApplicationError,
)
from corpuskit.domain.errors import (
    DependencyUnavailableError,
)
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.reproducibility import TrustedExecutionFacts
from corpuskit.services.artifact_adoption import ArtifactAdoptionError, ArtifactAdoptionService
from corpuskit.services.reproducibility import ReproducibilityError
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.deadlines import (
    PARENT_ACTIVITY_DEADLINE_CAP_SECONDS,
    activity_deadline_seconds,
)
from corpuskit.workflows.handlers import HandlerRegistry, RunExecutionError
from corpuskit.workflows.policies import (
    EXECUTE_ACTIVITY,
    EXECUTION_MAX_ATTEMPTS,
    FINALIZE_CANCELLATION_ACTIVITY,
    FINALIZE_FAILURE_ACTIVITY,
    PREPARE_ACTIVITY,
)
from corpuskit.workflows.process_runner import (
    PROCESS_EXECUTION_TIMEOUT_SECONDS,
    ProcessExecutionRunner,
)
from corpuskit.workflows.progress import DurableRunProgress
from corpuskit.workflows.store import DurableRunStore, RunStoreError
from corpuskit.workflows.trusted_inputs import TrustedRunInputMaterializer, TrustedRunInputs


class ExecutionFactsFactory(Protocol):
    def for_run(
        self,
        kind: RunKind,
        spec: Mapping[str, Any],
    ) -> TrustedExecutionFacts: ...


class RunManifestRecorder(Protocol):
    async def record_execution(
        self,
        reference: RunWorkflowReference,
        facts: TrustedExecutionFacts,
    ) -> bool: ...

    async def finalize(self, reference: RunWorkflowReference) -> object: ...


class CoreRunActivities:
    """Bound activities for the reviewed core handler registry."""

    def __init__(
        self,
        store: DurableRunStore,
        handlers: HandlerRegistry,
        *,
        heartbeat_seconds: float,
        artifact_adopter: ArtifactAdoptionService | None = None,
        activity_deadline_cap_seconds: float = PARENT_ACTIVITY_DEADLINE_CAP_SECONDS,
        process_hard_timeout_seconds: float = PROCESS_EXECUTION_TIMEOUT_SECONDS,
        execution_facts: ExecutionFactsFactory | None = None,
        manifest_recorder: RunManifestRecorder | None = None,
        input_materializer: TrustedRunInputMaterializer | None = None,
    ) -> None:
        if (execution_facts is None) != (manifest_recorder is None):
            raise ValueError("execution facts and manifest recorder must be configured together")
        self._store = store
        self._runner = ProcessExecutionRunner(
            handlers,
            hard_timeout_seconds=process_hard_timeout_seconds,
        )
        self._heartbeat_seconds = heartbeat_seconds
        self._artifact_adopter = artifact_adopter
        self._activity_deadline_cap_seconds = activity_deadline_seconds(
            RunKind.PHONEMIZE,
            {},
            server_cap_seconds=activity_deadline_cap_seconds,
        )
        self._execution_facts = execution_facts
        self._manifest_recorder = manifest_recorder
        self._input_materializer = input_materializer

    @activity.defn(name=PREPARE_ACTIVITY)
    async def prepare_run(self, reference: RunWorkflowReference) -> None:
        activity.heartbeat(reference)
        try:
            await self._store.prepare(reference)
        except RunStoreError as exc:
            raise _store_application_error(exc) from None
        except Exception:
            raise _safe_application_error("persistence_unavailable", retryable=True) from None

    @activity.defn(name=EXECUTE_ACTIVITY)
    async def execute_run(self, reference: RunWorkflowReference) -> None:
        activity.heartbeat(reference)
        try:
            record = await self._store.execution_record(reference)
            if record.state is RunState.SUCCEEDED:
                await self._finalize_manifest(reference)
                return
            if not await self._store.begin_execution(reference):
                return
            record = await self._store.execution_record(reference)
            await self._record_execution(reference, record.kind, record.spec)
            try:
                timeout_seconds = activity_deadline_seconds(
                    record.kind,
                    record.spec,
                    server_cap_seconds=self._activity_deadline_cap_seconds,
                )
            except ValueError:
                raise RunExecutionError("invalid_run_spec", retryable=False) from None
            if self._input_materializer is None:
                summary = await self._compute(
                    reference,
                    record.kind,
                    record.spec,
                    timeout_seconds=timeout_seconds,
                )
            else:
                async with self._input_materializer.materialize(record) as trusted_inputs:
                    summary = await self._compute(
                        reference,
                        record.kind,
                        record.spec,
                        timeout_seconds=timeout_seconds,
                        trusted_inputs=trusted_inputs,
                    )
            if await self._store.cancellation_requested(reference):
                await self._store.acknowledge_cancellation(reference)
                return
            if ArtifactAdoptionService.requires_adoption(record.kind):
                if self._artifact_adopter is None:
                    raise RunExecutionError(
                        "staged_result_adoption_unavailable",
                        retryable=True,
                    )
                committed = await self._artifact_adopter.adopt(reference, summary)
                succeeded = committed.state is RunState.SUCCEEDED
            else:
                if "staged_artifact_ref" in summary:
                    raise RunExecutionError("staged_result_unsupported", retryable=False)
                succeeded = (await self._store.complete(reference, summary)) is RunState.SUCCEEDED
            if succeeded:
                await self._finalize_manifest(reference)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self._store.acknowledge_cancellation(reference))
            except RunStoreError as exc:
                raise _store_application_error(exc) from None
            except Exception:
                raise _safe_application_error("persistence_unavailable", retryable=True) from None
            raise
        except RunExecutionError as exc:
            await self._handle_execution_failure(reference, exc)
        except ArtifactAdoptionError as exc:
            await self._handle_execution_failure(
                reference,
                RunExecutionError(exc.code, retryable=exc.retryable),
            )
        except RunStoreError as exc:
            raise _store_application_error(exc) from None
        except ApplicationError:
            raise
        except Exception:
            failure = RunExecutionError("internal_error", retryable=True)
            await self._handle_execution_failure(reference, failure)

    @activity.defn(name=FINALIZE_FAILURE_ACTIVITY)
    async def finalize_failure(self, reference: RunWorkflowReference) -> None:
        activity.heartbeat(reference)
        try:
            await self._store.fail(reference, "execution_exhausted")
        except RunStoreError as exc:
            raise _store_application_error(exc) from None
        except Exception:
            raise _safe_application_error("persistence_unavailable", retryable=True) from None

    @activity.defn(name=FINALIZE_CANCELLATION_ACTIVITY)
    async def finalize_cancellation(self, reference: RunWorkflowReference) -> None:
        activity.heartbeat(reference)
        try:
            await self._store.acknowledge_cancellation(reference)
        except RunStoreError as exc:
            raise _store_application_error(exc) from None
        except Exception:
            raise _safe_application_error("persistence_unavailable", retryable=True) from None

    async def _compute(
        self,
        reference: RunWorkflowReference,
        kind: RunKind,
        spec: dict[str, Any],
        *,
        timeout_seconds: float,
        trusted_inputs: TrustedRunInputs | None = None,
    ) -> dict[str, Any]:
        activity_attempt = activity.info().attempt

        async def persist_progress(progress: DurableRunProgress) -> None:
            activity.heartbeat(reference)
            if activity.is_cancelled() or await self._store.cancellation_requested(reference):
                await self._store.acknowledge_cancellation(reference)
                raise RunExecutionError("run_cancelled", retryable=False)
            recorded = await self._store.record_progress(
                reference,
                progress,
                activity_attempt=activity_attempt,
            )
            if not recorded and await self._store.cancellation_requested(reference):
                await self._store.acknowledge_cancellation(reference)
                raise RunExecutionError("run_cancelled", retryable=False)

        async def tick() -> None:
            activity.heartbeat(reference)
            if activity.is_cancelled() or await self._store.cancellation_requested(reference):
                await self._store.acknowledge_cancellation(reference)
                raise RunExecutionError("run_cancelled", retryable=False)
            if activity.is_worker_shutdown():
                raise RunExecutionError("worker_shutdown", retryable=True)

        try:
            return await self._runner.execute(
                kind,
                spec,
                tick=tick,
                tick_seconds=self._heartbeat_seconds,
                timeout_seconds=timeout_seconds,
                on_progress=persist_progress,
                trusted_inputs=(
                    trusted_inputs.model_dump(mode="json") if trusted_inputs is not None else None
                ),
            )
        except RunExecutionError as exc:
            if exc.code == "run_cancelled":
                return {}
            raise

    async def _handle_execution_failure(
        self,
        reference: RunWorkflowReference,
        failure: RunExecutionError,
    ) -> None:
        final_attempt = activity.info().attempt >= EXECUTION_MAX_ATTEMPTS
        if not failure.retryable or final_attempt:
            try:
                await self._store.fail(reference, failure.code)
            except RunStoreError as exc:
                raise _store_application_error(exc) from None
            except Exception:
                raise _safe_application_error("persistence_unavailable", retryable=True) from None
        raise _safe_application_error(
            failure.code,
            retryable=failure.retryable and not final_attempt,
        )

    async def _record_execution(
        self,
        reference: RunWorkflowReference,
        kind: RunKind,
        spec: Mapping[str, Any],
    ) -> None:
        if self._execution_facts is None or self._manifest_recorder is None:
            return
        try:
            facts = self._execution_facts.for_run(kind, spec)
            await self._manifest_recorder.record_execution(reference, facts)
        except DependencyUnavailableError:
            raise RunExecutionError(
                "manifest_storage_unavailable",
                retryable=True,
            ) from None
        except DomainApplicationError as exc:
            raise RunExecutionError(
                exc.code.value,
                retryable=False,
            ) from None
        except ReproducibilityError as exc:
            raise RunExecutionError(exc.code, retryable=False) from None
        except ValueError:
            raise RunExecutionError("invalid_run_spec", retryable=False) from None

    async def _finalize_manifest(self, reference: RunWorkflowReference) -> None:
        if self._manifest_recorder is None:
            return
        try:
            await self._manifest_recorder.finalize(reference)
        except DependencyUnavailableError:
            raise RunExecutionError(
                "manifest_storage_unavailable",
                retryable=True,
            ) from None
        except DomainApplicationError as exc:
            raise RunExecutionError(exc.code.value, retryable=False) from None
        except ReproducibilityError as exc:
            raise RunExecutionError(exc.code, retryable=False) from None


def _store_application_error(error: RunStoreError) -> ApplicationError:
    non_retryable = error.code in {
        "invalid_workflow_reference",
        "run_not_found",
        "spec_integrity_violation",
        "invalid_run_state",
    }
    return _safe_application_error(error.code, retryable=not non_retryable)


def _safe_application_error(code: str, *, retryable: bool) -> ApplicationError:
    return ApplicationError(
        "CorpusKit durable execution could not complete.",
        type=code,
        non_retryable=not retryable,
    )


__all__ = ["CoreRunActivities"]
