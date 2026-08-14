"""Atomic tenant quota accounting and immutable audit evidence services."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.domain.artifacts import ArtifactKind
from corpuskit.domain.errors import (
    InvalidRequestError,
    QuotaExceededError,
    ResourceNotFoundError,
)
from corpuskit.domain.jobs import RunState, ensure_transition, is_terminal
from corpuskit.domain.platform import (
    AUDIT_GENESIS_HASH,
    AuditAction,
    AuditActorKind,
    AuditResourceType,
    QuotaPolicyValues,
    QuotaReservationState,
    RunQuotaClass,
    audit_event_hash,
    normalize_audit_metadata,
    run_quota_class,
    safe_correlation_id,
    validate_run_resource_policy,
)
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import (
    AuditEvent,
    AuditHead,
    Membership,
    QuotaPolicy,
    QuotaReservation,
    QuotaUsage,
    Role,
    Run,
    RunEvent,
    User,
)
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext

_HASH = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_ADMIN_ROLES = frozenset({Role.OWNER, Role.ADMIN})
_DEFAULT_RUN_DEADLINE_SECONDS = 300.0
_JOB_RESERVATION_GRACE_SECONDS = 5 * 60


@dataclass(frozen=True, slots=True)
class AuditIdentity:
    kind: AuditActorKind
    identifier: str

    @classmethod
    def user(cls, user_id: UUID) -> AuditIdentity:
        return cls(AuditActorKind.USER, str(user_id))

    @classmethod
    def service(cls, identity: ServiceIdentity) -> AuditIdentity:
        if identity is ServiceIdentity.USER:
            raise ValueError("service audit identity cannot be a user")
        return cls(AuditActorKind.SERVICE, f"service:{identity.value}")


@dataclass(frozen=True, slots=True)
class QuotaUsageSnapshot:
    active_cpu_jobs: int
    active_expensive_jobs: int
    artifact_bytes: int
    artifact_count: int
    corpus_sentences: int


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    policy: QuotaPolicyValues
    usage: QuotaUsageSnapshot


@dataclass(frozen=True, slots=True)
class AuditEventSnapshot:
    sequence: int
    actor_kind: AuditActorKind
    actor_id: str
    action: AuditAction
    resource_type: AuditResourceType
    resource_id: UUID
    request_id: str | None
    occurred_at: datetime
    metadata: dict[str, Any]
    previous_hash: str
    event_hash: str


@dataclass(frozen=True, slots=True)
class AuditPage:
    events: tuple[AuditEventSnapshot, ...]
    next_cursor: str | None


class AuditWriter:
    """Append exactly one allowlisted event while holding the organization chain head."""

    @staticmethod
    async def append(
        session: AsyncSession,
        *,
        organization_id: UUID,
        actor: AuditIdentity,
        action: AuditAction,
        resource_type: AuditResourceType,
        resource_id: UUID,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditEvent:
        normalized_metadata = normalize_audit_metadata(action, metadata or {})
        safe_request_id = safe_correlation_id(request_id)
        await _ensure_platform_rows(session, organization_id)
        head = await session.scalar(
            select(AuditHead).where(AuditHead.organization_id == organization_id).with_for_update()
        )
        if head is None or _HASH.fullmatch(head.last_hash) is None:
            raise RuntimeError("audit chain head is unavailable")
        sequence = head.last_sequence + 1
        timestamp = (occurred_at or datetime.now(UTC)).astimezone(UTC)
        event_hash = audit_event_hash(
            organization_id=organization_id,
            sequence=sequence,
            actor_kind=actor.kind,
            actor_id=actor.identifier,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=safe_request_id,
            occurred_at=timestamp,
            metadata=normalized_metadata,
            previous_hash=head.last_hash,
        )
        event = AuditEvent(
            id=uuid4(),
            organization_id=organization_id,
            sequence=sequence,
            actor_kind=actor.kind,
            actor_id=actor.identifier,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=safe_request_id,
            occurred_at=timestamp,
            details=normalized_metadata,
            previous_hash=head.last_hash,
            event_hash=event_hash,
        )
        session.add(event)
        head.last_sequence = sequence
        head.last_hash = event_hash
        head.updated_at = timestamp
        await session.flush()
        return event

    @staticmethod
    async def verify(session: AsyncSession, organization_id: UUID) -> bool:
        head = await session.scalar(
            select(AuditHead).where(AuditHead.organization_id == organization_id)
        )
        if head is None:
            return False
        previous_hash = AUDIT_GENESIS_HASH
        expected_sequence = 1
        events = await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == organization_id)
            .order_by(AuditEvent.sequence)
        )
        for event in events:
            if event.sequence != expected_sequence or event.previous_hash != previous_hash:
                return False
            expected_hash = audit_event_hash(
                organization_id=event.organization_id,
                sequence=event.sequence,
                actor_kind=event.actor_kind,
                actor_id=event.actor_id,
                action=event.action,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                request_id=event.request_id,
                occurred_at=event.occurred_at,
                metadata=dict(event.details),
                previous_hash=event.previous_hash,
            )
            if event.event_hash != expected_hash:
                return False
            previous_hash = event.event_hash
            expected_sequence += 1
        return head.last_sequence == expected_sequence - 1 and head.last_hash == previous_hash


class QuotaManager:
    """Mutate usage only inside the caller's resource transaction."""

    @staticmethod
    async def ensure_tenant(session: AsyncSession, organization_id: UUID) -> None:
        await _ensure_platform_rows(session, organization_id)

    @staticmethod
    async def expire_stale(
        database: Database,
        *,
        now: datetime | None = None,
        limit: int = 1_000,
    ) -> int:
        if not 1 <= limit <= 10_000:
            raise ValueError("quota expiry limit must be between 1 and 10000")
        cutoff = (now or datetime.now(UTC)).astimezone(UTC)
        global_context = TenantContext.service(ServiceIdentity.MAINTENANCE)
        async with database.session(global_context) as session:
            candidates = tuple(
                (
                    await session.execute(
                        select(
                            QuotaReservation.id,
                            QuotaReservation.organization_id,
                            QuotaReservation.run_id,
                        )
                        .where(
                            QuotaReservation.state == QuotaReservationState.ACTIVE,
                            QuotaReservation.expires_at <= cutoff,
                        )
                        .order_by(QuotaReservation.expires_at, QuotaReservation.id)
                        .limit(limit)
                    )
                ).all()
            )
        expired = 0
        for reservation_id, organization_id, run_id in candidates:
            context = TenantContext.service(ServiceIdentity.MAINTENANCE, organization_id)
            async with database.session(context) as session:
                run = await session.scalar(
                    select(Run)
                    .where(
                        Run.id == run_id,
                        Run.organization_id == organization_id,
                    )
                    .with_for_update()
                )
                if run is None:
                    raise RuntimeError("quota reservation run is unavailable")
                reservation = await session.scalar(
                    select(QuotaReservation)
                    .where(
                        QuotaReservation.id == reservation_id,
                        QuotaReservation.organization_id == organization_id,
                        QuotaReservation.state == QuotaReservationState.ACTIVE,
                        QuotaReservation.expires_at <= cutoff,
                    )
                    .with_for_update()
                )
                if reservation is None:
                    continue
                terminal_action: AuditAction | None = None
                if not is_terminal(run.state):
                    if run.state is RunState.CANCELLING:
                        target = RunState.CANCELLED
                        terminal_action = AuditAction.RUN_CANCELLED
                        failure_code = None
                    else:
                        target = RunState.FAILED
                        terminal_action = AuditAction.RUN_FAILED
                        failure_code = "quota_reservation_expired"
                    ensure_transition(run.state, target)
                    run.state = target
                    run.event_sequence += 1
                    run.result_summary = None
                    run.failure_code = failure_code
                    session.add(
                        RunEvent(
                            organization_id=organization_id,
                            run_id=run.id,
                            sequence=run.event_sequence,
                            event_type=(
                                "run.cancelled" if target is RunState.CANCELLED else "run.failed"
                            ),
                            payload=(
                                {"state": target.value}
                                if failure_code is None
                                else {"failure_code": failure_code, "state": target.value}
                            ),
                        )
                    )
                released = await QuotaManager.release_run(
                    session,
                    organization_id=organization_id,
                    run_id=run_id,
                    state=QuotaReservationState.EXPIRED,
                )
                if not released:
                    continue
                await AuditWriter.append(
                    session,
                    organization_id=organization_id,
                    actor=AuditIdentity.service(ServiceIdentity.MAINTENANCE),
                    action=AuditAction.QUOTA_RESERVATION_EXPIRED,
                    resource_type=AuditResourceType.QUOTA_RESERVATION,
                    resource_id=reservation_id,
                    metadata={
                        "kind": run.kind.value,
                        "quota_class": reservation.quota_class.value,
                    },
                )
                if terminal_action is not None:
                    terminal_metadata: dict[str, Any] = {"kind": run.kind.value}
                    if terminal_action is AuditAction.RUN_FAILED:
                        terminal_metadata["failure_code"] = "quota_reservation_expired"
                    await AuditWriter.append(
                        session,
                        organization_id=organization_id,
                        actor=AuditIdentity.service(ServiceIdentity.MAINTENANCE),
                        action=terminal_action,
                        resource_type=AuditResourceType.RUN,
                        resource_id=run.id,
                        metadata=terminal_metadata,
                    )
                expired += 1
        return expired

    @staticmethod
    async def reserve_run(
        session: AsyncSession,
        *,
        organization_id: UUID,
        run: Run,
    ) -> bool:
        existing = await session.scalar(
            select(QuotaReservation).where(
                QuotaReservation.organization_id == organization_id,
                QuotaReservation.run_id == run.id,
            )
        )
        quota_class = run_quota_class(run.kind)
        if existing is not None:
            if (
                existing.quota_class is quota_class
                and existing.state is QuotaReservationState.ACTIVE
            ):
                return False
            raise RuntimeError("run quota reservation is inconsistent")

        policy, usage = await QuotaManager._locked(session, organization_id)
        values = _policy_values(policy)
        try:
            deadline_seconds = validate_run_resource_policy(run.kind, dict(run.spec), values)
        except ValidationError as exc:
            raise InvalidRequestError("run.submit") from exc
        except ValueError as exc:
            raise QuotaExceededError("run.submit") from exc

        if quota_class is RunQuotaClass.CPU:
            if usage.active_cpu_jobs >= policy.max_concurrent_cpu_jobs:
                raise QuotaExceededError("run.submit", retry_after_seconds=30)
            usage.active_cpu_jobs += 1
        else:
            if usage.active_expensive_jobs >= policy.max_concurrent_expensive_jobs:
                raise QuotaExceededError("run.submit", retry_after_seconds=30)
            usage.active_expensive_jobs += 1
        now = datetime.now(UTC)
        session.add(
            QuotaReservation(
                organization_id=organization_id,
                run_id=run.id,
                quota_class=quota_class,
                amount=1,
                state=QuotaReservationState.ACTIVE,
                expires_at=now
                + timedelta(
                    seconds=(deadline_seconds or _DEFAULT_RUN_DEADLINE_SECONDS)
                    + _JOB_RESERVATION_GRACE_SECONDS
                ),
            )
        )
        usage.updated_at = now
        await session.flush()
        return True

    @staticmethod
    async def renew_run(
        session: AsyncSession,
        *,
        organization_id: UUID,
        run: Run,
    ) -> bool:
        reservation = await session.scalar(
            select(QuotaReservation)
            .where(
                QuotaReservation.organization_id == organization_id,
                QuotaReservation.run_id == run.id,
            )
            .with_for_update()
        )
        if reservation is None or reservation.state is not QuotaReservationState.ACTIVE:
            return False
        policy = await session.scalar(
            select(QuotaPolicy).where(QuotaPolicy.organization_id == organization_id)
        )
        if policy is None:
            raise RuntimeError("quota policy is unavailable")
        try:
            deadline_seconds = validate_run_resource_policy(
                run.kind,
                dict(run.spec),
                _policy_values(policy),
            )
        except (ValidationError, ValueError) as exc:
            raise RuntimeError("persisted run no longer satisfies quota policy") from exc
        reservation.expires_at = datetime.now(UTC) + timedelta(
            seconds=(deadline_seconds or _DEFAULT_RUN_DEADLINE_SECONDS)
            + _JOB_RESERVATION_GRACE_SECONDS
        )
        await session.flush()
        return True

    @staticmethod
    async def release_run(
        session: AsyncSession,
        *,
        organization_id: UUID,
        run_id: UUID,
        state: QuotaReservationState = QuotaReservationState.RELEASED,
    ) -> bool:
        reservation = await session.scalar(
            select(QuotaReservation)
            .where(
                QuotaReservation.organization_id == organization_id,
                QuotaReservation.run_id == run_id,
            )
            .with_for_update()
        )
        if reservation is None or reservation.state is not QuotaReservationState.ACTIVE:
            return False
        _, usage = await QuotaManager._locked(session, organization_id)
        if reservation.quota_class is RunQuotaClass.CPU:
            if usage.active_cpu_jobs <= 0:
                raise RuntimeError("CPU quota usage is inconsistent")
            usage.active_cpu_jobs -= 1
        else:
            if usage.active_expensive_jobs <= 0:
                raise RuntimeError("expensive quota usage is inconsistent")
            usage.active_expensive_jobs -= 1
        now = datetime.now(UTC)
        usage.updated_at = now
        reservation.state = state
        reservation.released_at = now
        await session.flush()
        return True

    @staticmethod
    async def consume_artifact(
        session: AsyncSession,
        *,
        organization_id: UUID,
        kind: ArtifactKind,
        size_bytes: int,
    ) -> None:
        if size_bytes <= 0:
            raise ValueError("artifact usage must be positive")
        policy, usage = await QuotaManager._locked(session, organization_id)
        if kind in {ArtifactKind.CHECKPOINT, ArtifactKind.MODEL_ADAPTER} and (
            size_bytes > policy.max_checkpoint_bytes
        ):
            raise QuotaExceededError("artifact.create")
        if usage.artifact_count + 1 > policy.max_artifact_count:
            raise QuotaExceededError("artifact.create")
        if usage.artifact_bytes + size_bytes > policy.max_artifact_bytes:
            raise QuotaExceededError("artifact.create")
        usage.artifact_count += 1
        usage.artifact_bytes += size_bytes
        usage.updated_at = datetime.now(UTC)
        await session.flush()

    @staticmethod
    async def release_artifact(
        session: AsyncSession,
        *,
        organization_id: UUID,
        size_bytes: int,
    ) -> None:
        _, usage = await QuotaManager._locked(session, organization_id)
        if usage.artifact_count <= 0 or usage.artifact_bytes < size_bytes:
            raise RuntimeError("artifact quota usage is inconsistent")
        usage.artifact_count -= 1
        usage.artifact_bytes -= size_bytes
        usage.updated_at = datetime.now(UTC)
        await session.flush()

    @staticmethod
    async def consume_corpus_sentences(
        session: AsyncSession,
        *,
        organization_id: UUID,
        sentence_count: int,
        operation: str = "corpus.create",
    ) -> None:
        if sentence_count <= 0:
            raise ValueError("corpus sentence usage must be positive")
        policy, usage = await QuotaManager._locked(session, organization_id)
        if usage.corpus_sentences + sentence_count > policy.max_corpus_sentences:
            raise QuotaExceededError(operation)
        usage.corpus_sentences += sentence_count
        usage.updated_at = datetime.now(UTC)
        await session.flush()

    @staticmethod
    async def release_corpus_sentences(
        session: AsyncSession,
        *,
        organization_id: UUID,
        sentence_count: int,
    ) -> None:
        """Release an exactly accounted project corpus allocation during final erasure."""

        if sentence_count <= 0:
            raise ValueError("corpus sentence release must be positive")
        _, usage = await QuotaManager._locked(session, organization_id)
        if usage.corpus_sentences < sentence_count:
            raise RuntimeError("corpus sentence quota usage is inconsistent")
        usage.corpus_sentences -= sentence_count
        usage.updated_at = datetime.now(UTC)
        await session.flush()

    @staticmethod
    async def snapshot(session: AsyncSession, organization_id: UUID) -> QuotaSnapshot:
        policy, usage = await QuotaManager._locked(session, organization_id)
        return QuotaSnapshot(
            policy=_policy_values(policy),
            usage=QuotaUsageSnapshot(
                active_cpu_jobs=usage.active_cpu_jobs,
                active_expensive_jobs=usage.active_expensive_jobs,
                artifact_bytes=usage.artifact_bytes,
                artifact_count=usage.artifact_count,
                corpus_sentences=usage.corpus_sentences,
            ),
        )

    @staticmethod
    async def _locked(
        session: AsyncSession,
        organization_id: UUID,
    ) -> tuple[QuotaPolicy, QuotaUsage]:
        await _ensure_platform_rows(session, organization_id)
        policy = await session.scalar(
            select(QuotaPolicy).where(QuotaPolicy.organization_id == organization_id)
        )
        usage = await session.scalar(
            select(QuotaUsage)
            .where(QuotaUsage.organization_id == organization_id)
            .with_for_update()
        )
        if policy is None or usage is None:
            raise RuntimeError("quota state is unavailable")
        return policy, usage


@dataclass(frozen=True, slots=True)
class PlatformActor:
    subject: str
    organization_id: UUID
    request_id: str | None = None


class PlatformService:
    """Owner/admin read surface and service-only policy mutation boundary."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def quota(self, actor: PlatformActor) -> QuotaSnapshot:
        context = TenantContext.user(actor.organization_id, actor.subject)
        async with self.database.session(context) as session:
            await _admin_actor(session, actor)
            return await QuotaManager.snapshot(session, actor.organization_id)

    async def audit_events(
        self,
        actor: PlatformActor,
        *,
        cursor: str | None = None,
        limit: int = 100,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        action: AuditAction | None = None,
        resource_type: AuditResourceType | None = None,
    ) -> AuditPage:
        if not 1 <= limit <= 200:
            raise InvalidRequestError("audit.list")
        after = _audit_cursor(cursor)
        if occurred_from is not None and occurred_from.tzinfo is None:
            raise InvalidRequestError("audit.list")
        if occurred_to is not None and occurred_to.tzinfo is None:
            raise InvalidRequestError("audit.list")
        if occurred_from is not None and occurred_to is not None and occurred_from > occurred_to:
            raise InvalidRequestError("audit.list")
        context = TenantContext.user(actor.organization_id, actor.subject)
        async with self.database.session(context) as session:
            await _admin_actor(session, actor)
            statement: Select[tuple[AuditEvent]] = select(AuditEvent).where(
                AuditEvent.organization_id == actor.organization_id,
                AuditEvent.sequence > after,
            )
            if occurred_from is not None:
                statement = statement.where(AuditEvent.occurred_at >= occurred_from)
            if occurred_to is not None:
                statement = statement.where(AuditEvent.occurred_at <= occurred_to)
            if action is not None:
                statement = statement.where(AuditEvent.action == action)
            if resource_type is not None:
                statement = statement.where(AuditEvent.resource_type == resource_type)
            rows = tuple(
                await session.scalars(statement.order_by(AuditEvent.sequence).limit(limit + 1))
            )
            visible = rows[:limit]
            next_cursor = str(visible[-1].sequence) if len(rows) > limit and visible else None
            return AuditPage(
                events=tuple(_audit_snapshot(event) for event in visible),
                next_cursor=next_cursor,
            )

    async def replace_policy(
        self,
        context: TenantContext,
        *,
        organization_id: UUID,
        policy: QuotaPolicyValues,
        request_id: str | None = None,
    ) -> QuotaSnapshot:
        if (
            context.identity is not ServiceIdentity.PLATFORM
            or context.organization_id != organization_id
        ):
            raise ResourceNotFoundError("quota.policy.update")
        async with self.database.session(context) as session:
            current, usage = await QuotaManager._locked(session, organization_id)
            if (
                usage.active_cpu_jobs > policy.max_concurrent_cpu_jobs
                or usage.active_expensive_jobs > policy.max_concurrent_expensive_jobs
                or usage.artifact_bytes > policy.max_artifact_bytes
                or usage.artifact_count > policy.max_artifact_count
                or usage.corpus_sentences > policy.max_corpus_sentences
            ):
                raise QuotaExceededError("quota.policy.update")
            active_runs = await session.scalars(
                select(Run)
                .join(
                    QuotaReservation,
                    QuotaReservation.run_id == Run.id,
                )
                .where(
                    Run.organization_id == organization_id,
                    QuotaReservation.organization_id == organization_id,
                    QuotaReservation.state == QuotaReservationState.ACTIVE,
                )
            )
            try:
                for run in active_runs:
                    validate_run_resource_policy(run.kind, dict(run.spec), policy)
            except (ValidationError, ValueError) as exc:
                raise QuotaExceededError("quota.policy.update") from exc
            before = _policy_values(current)
            for field, value in policy.model_dump().items():
                setattr(current, field, value)
            current.updated_at = datetime.now(UTC)
            changed_fields = sorted(
                field
                for field in QuotaPolicyValues.model_fields
                if getattr(before, field) != getattr(policy, field)
            )
            await AuditWriter.append(
                session,
                organization_id=organization_id,
                actor=AuditIdentity.service(ServiceIdentity.PLATFORM),
                action=AuditAction.QUOTA_POLICY_CHANGED,
                resource_type=AuditResourceType.QUOTA_POLICY,
                resource_id=organization_id,
                request_id=request_id,
                metadata={"changed_fields": changed_fields},
            )
            return await QuotaManager.snapshot(session, organization_id)


async def _ensure_platform_rows(session: AsyncSession, organization_id: UUID) -> None:
    values = QuotaPolicyValues()
    policy_values = {"organization_id": organization_id, **values.model_dump()}
    usage_values = {"organization_id": organization_id}
    audit_values = {
        "organization_id": organization_id,
        "last_sequence": 0,
        "last_hash": AUDIT_GENESIS_HASH,
    }
    dialect = session.get_bind().dialect.name
    statements: tuple[Any, ...]
    if dialect == "postgresql":
        context = session.info.get("tenant_context")
        if (
            not isinstance(context, TenantContext)
            or context.identity is not ServiceIdentity.PLATFORM
        ):
            return
        statements = (
            postgresql_insert(QuotaPolicy).values(**policy_values).on_conflict_do_nothing(),
            postgresql_insert(QuotaUsage).values(**usage_values).on_conflict_do_nothing(),
            postgresql_insert(AuditHead).values(**audit_values).on_conflict_do_nothing(),
        )
    elif dialect == "sqlite":
        statements = (
            sqlite_insert(QuotaPolicy).values(**policy_values).on_conflict_do_nothing(),
            sqlite_insert(QuotaUsage).values(**usage_values).on_conflict_do_nothing(),
            sqlite_insert(AuditHead).values(**audit_values).on_conflict_do_nothing(),
        )
    else:
        raise RuntimeError("tenant quota state requires PostgreSQL or SQLite")
    for statement in statements:
        await session.execute(statement)
    await session.flush()


async def _admin_actor(session: AsyncSession, actor: PlatformActor) -> tuple[UUID, Role]:
    row = (
        await session.execute(
            select(User.id, Membership.role)
            .join(Membership, Membership.user_id == User.id)
            .where(
                User.oidc_subject == actor.subject,
                Membership.organization_id == actor.organization_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise ResourceNotFoundError("platform.identity")
    user_id, role = row._tuple()
    if role not in _ADMIN_ROLES:
        raise ResourceNotFoundError("platform.identity")
    return user_id, role


def _policy_values(policy: QuotaPolicy) -> QuotaPolicyValues:
    return QuotaPolicyValues(
        **{field: getattr(policy, field) for field in QuotaPolicyValues.model_fields}
    )


def _audit_cursor(value: str | None) -> int:
    if value is None:
        return 0
    if re.fullmatch(r"[1-9][0-9]{0,18}", value, re.ASCII) is None:
        raise InvalidRequestError("audit.list")
    return int(value)


def _audit_snapshot(event: AuditEvent) -> AuditEventSnapshot:
    return AuditEventSnapshot(
        sequence=event.sequence,
        actor_kind=event.actor_kind,
        actor_id=event.actor_id,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        request_id=event.request_id,
        occurred_at=event.occurred_at,
        metadata=dict(event.details),
        previous_hash=event.previous_hash,
        event_hash=event.event_hash,
    )


__all__ = [
    "AuditEventSnapshot",
    "AuditIdentity",
    "AuditPage",
    "AuditWriter",
    "PlatformActor",
    "PlatformService",
    "QuotaManager",
    "QuotaSnapshot",
    "QuotaUsageSnapshot",
]
