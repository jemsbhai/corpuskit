"""Versioned Temporal names, deadlines, and retry policies."""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

WORKFLOW_NAME = "corpuskit.core-run.v1"
CANCELLATION_SIGNAL = "request-cancellation"
PREPARE_ACTIVITY = "corpuskit.prepare-run.v1"
EXECUTE_ACTIVITY = "corpuskit.execute-run.v1"
FINALIZE_FAILURE_ACTIVITY = "corpuskit.finalize-failure.v1"
FINALIZE_CANCELLATION_ACTIVITY = "corpuskit.finalize-cancellation.v1"

PREPARE_TIMEOUT = timedelta(seconds=30)
EXECUTION_SCHEDULE_TIMEOUT = timedelta(hours=73)
EXECUTION_START_TIMEOUT = timedelta(hours=25)
EXECUTION_HEARTBEAT_TIMEOUT = timedelta(seconds=15)
FINALIZE_TIMEOUT = timedelta(seconds=30)
WORKFLOW_RUN_TIMEOUT = timedelta(hours=74)
WORKFLOW_EXECUTION_TIMEOUT = timedelta(hours=74)
WORKER_GRACEFUL_SHUTDOWN_TIMEOUT = timedelta(seconds=30)

CONTROL_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=5),
    maximum_attempts=5,
    non_retryable_error_types=("invalid_workflow_reference", "run_not_found"),
)
EXECUTION_MAX_ATTEMPTS = 3
EXECUTION_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=EXECUTION_MAX_ATTEMPTS,
    non_retryable_error_types=(
        "invalid_run_spec",
        "unsupported_run_kind",
        "language_not_supported",
        "inventory_not_found",
        "inventory_data_unavailable",
        "dependency_unavailable",
        "engine_contract_violation",
        "spec_integrity_violation",
        "run_not_found",
    ),
)

SUPPORTED_CORE_KINDS = frozenset(
    {
        "phonemize",
        "evaluate",
        "distribution",
        "trajectory",
        "error-rates",
        "select",
    }
)

__all__ = [
    "CANCELLATION_SIGNAL",
    "CONTROL_RETRY_POLICY",
    "EXECUTE_ACTIVITY",
    "EXECUTION_HEARTBEAT_TIMEOUT",
    "EXECUTION_MAX_ATTEMPTS",
    "EXECUTION_RETRY_POLICY",
    "EXECUTION_SCHEDULE_TIMEOUT",
    "EXECUTION_START_TIMEOUT",
    "FINALIZE_CANCELLATION_ACTIVITY",
    "FINALIZE_FAILURE_ACTIVITY",
    "FINALIZE_TIMEOUT",
    "PREPARE_ACTIVITY",
    "PREPARE_TIMEOUT",
    "SUPPORTED_CORE_KINDS",
    "WORKER_GRACEFUL_SHUTDOWN_TIMEOUT",
    "WORKFLOW_EXECUTION_TIMEOUT",
    "WORKFLOW_NAME",
    "WORKFLOW_RUN_TIMEOUT",
]
