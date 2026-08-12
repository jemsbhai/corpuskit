"""Add forced tenant RLS, atomic quotas, and immutable audit evidence.

Revision ID: 0004_tenant_controls
Revises: 0003_artifact_integrity
Created: 2026-08-11 20:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0004_tenant_controls"
down_revision: str | None = "0003_artifact_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GENESIS_HASH = "0" * 64
_CPU_KINDS = {
    "PHONEMIZE",
    "EVALUATE",
    "DISTRIBUTION",
    "TRAJECTORY",
    "ERROR_RATES",
    "SELECT",
    "GENERATE_REPOSITORY",
    "BUILD_DATG_INDEX",
    "EXPORT",
}
_EXPENSIVE_KINDS = {
    "PERPLEXITY",
    "GENERATE_LLM",
    "GENERATE_LOCAL",
    "GENERATE_DATG",
    "TRAIN_PHON_RL",
}


def _id() -> sa.Uuid[UUID]:
    return sa.Uuid(as_uuid=True)


def _timestamp(name: str, *, default: bool = False) -> sa.Column[datetime]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default else None,
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "quota_policies",
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("max_concurrent_cpu_jobs", sa.Integer(), nullable=False),
        sa.Column("max_concurrent_expensive_jobs", sa.Integer(), nullable=False),
        sa.Column("max_artifact_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_artifact_count", sa.Integer(), nullable=False),
        sa.Column("max_corpus_sentences", sa.BigInteger(), nullable=False),
        sa.Column("max_generation_accepted_sentences", sa.Integer(), nullable=False),
        sa.Column("max_generation_iterations", sa.Integer(), nullable=False),
        sa.Column("max_activity_deadline_seconds", sa.Float(), nullable=False),
        sa.Column("max_provider_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("max_provider_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("max_provider_cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("max_rl_steps", sa.Integer(), nullable=False),
        sa.Column("max_rl_tokens", sa.BigInteger(), nullable=False),
        sa.Column("max_checkpoint_bytes", sa.BigInteger(), nullable=False),
        _timestamp("updated_at", default=True),
        sa.CheckConstraint(
            "max_concurrent_cpu_jobs > 0", name=op.f("ck_quota_policies_positive_cpu_limit")
        ),
        sa.CheckConstraint(
            "max_concurrent_expensive_jobs > 0",
            name=op.f("ck_quota_policies_positive_expensive_limit"),
        ),
        sa.CheckConstraint(
            "max_artifact_bytes > 0",
            name=op.f("ck_quota_policies_positive_artifact_bytes_limit"),
        ),
        sa.CheckConstraint(
            "max_artifact_count > 0",
            name=op.f("ck_quota_policies_positive_artifact_count_limit"),
        ),
        sa.CheckConstraint(
            "max_corpus_sentences > 0",
            name=op.f("ck_quota_policies_positive_corpus_sentence_limit"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_quota_policies_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id", name="pk_quota_policies"),
    )
    op.create_table(
        "quota_usages",
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("active_cpu_jobs", sa.Integer(), nullable=False),
        sa.Column("active_expensive_jobs", sa.Integer(), nullable=False),
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=False),
        sa.Column("artifact_count", sa.Integer(), nullable=False),
        sa.Column("corpus_sentences", sa.BigInteger(), nullable=False),
        _timestamp("updated_at", default=True),
        sa.CheckConstraint(
            "active_cpu_jobs >= 0", name=op.f("ck_quota_usages_nonnegative_active_cpu_jobs")
        ),
        sa.CheckConstraint(
            "active_expensive_jobs >= 0",
            name=op.f("ck_quota_usages_nonnegative_active_expensive_jobs"),
        ),
        sa.CheckConstraint(
            "artifact_bytes >= 0", name=op.f("ck_quota_usages_nonnegative_artifact_bytes")
        ),
        sa.CheckConstraint(
            "artifact_count >= 0", name=op.f("ck_quota_usages_nonnegative_artifact_count")
        ),
        sa.CheckConstraint(
            "corpus_sentences >= 0", name=op.f("ck_quota_usages_nonnegative_corpus_sentences")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_quota_usages_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id", name="pk_quota_usages"),
    )
    op.create_table(
        "quota_reservations",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("run_id", _id(), nullable=False),
        sa.Column(
            "quota_class",
            sa.Enum("CPU", "EXPENSIVE", name="runquotaclass", native_enum=False),
            nullable=False,
        ),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "ACTIVE", "RELEASED", "EXPIRED", name="quotareservationstate", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("created_at", default=True),
        sa.CheckConstraint("amount = 1", name=op.f("ck_quota_reservations_unit_amount")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_quota_reservations_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name="fk_quota_reservations_run_id_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quota_reservations"),
        sa.UniqueConstraint(
            "organization_id", "run_id", name="uq_quota_reservations_organization_run"
        ),
    )
    op.create_index(
        "ix_quota_reservations_organization_id", "quota_reservations", ["organization_id"]
    )
    op.create_index("ix_quota_reservations_run_id", "quota_reservations", ["run_id"])
    op.create_index("ix_quota_reservations_expiry", "quota_reservations", ["state", "expires_at"])
    op.create_table(
        "audit_heads",
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_hash", sa.String(length=64), nullable=False),
        _timestamp("updated_at", default=True),
        sa.CheckConstraint(
            "last_sequence >= 0", name=op.f("ck_audit_heads_nonnegative_last_sequence")
        ),
        sa.CheckConstraint("length(last_hash) = 64", name=op.f("ck_audit_heads_last_hash_length")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_audit_heads_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id", name="pk_audit_heads"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column(
            "actor_kind",
            sa.Enum("USER", "SERVICE", name="auditactorkind", native_enum=False),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "PROJECT_CREATED",
                "CORPUS_CREATED",
                "RUN_SUBMITTED",
                "RUN_CANCELLATION_REQUESTED",
                "RUN_RETRY_SUBMITTED",
                "RUN_SUCCEEDED",
                "RUN_FAILED",
                "RUN_CANCELLED",
                "ARTIFACT_CREATED",
                "ARTIFACT_TOMBSTONED",
                "ARTIFACT_PURGED",
                "ARTIFACT_ADOPTED",
                "QUOTA_POLICY_CHANGED",
                "QUOTA_RESERVATION_EXPIRED",
                name="auditaction",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "resource_type",
            sa.Enum(
                "PROJECT",
                "CORPUS",
                "RUN",
                "ARTIFACT",
                "QUOTA_POLICY",
                "QUOTA_RESERVATION",
                name="auditresourcetype",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("resource_id", _id(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=False),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint("sequence > 0", name=op.f("ck_audit_events_positive_sequence")),
        sa.CheckConstraint(
            "length(previous_hash) = 64", name=op.f("ck_audit_events_previous_hash_length")
        ),
        sa.CheckConstraint(
            "length(event_hash) = 64", name=op.f("ck_audit_events_event_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_audit_events_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.UniqueConstraint(
            "organization_id", "sequence", name="uq_audit_events_organization_sequence"
        ),
    )
    op.create_index("ix_audit_events_organization_id", "audit_events", ["organization_id"])
    op.create_index(
        "ix_audit_events_org_time", "audit_events", ["organization_id", "occurred_at", "sequence"]
    )
    op.create_index(
        "ix_audit_events_org_action", "audit_events", ["organization_id", "action", "sequence"]
    )

    _backfill_platform_state()
    if op.get_bind().dialect.name == "postgresql":
        _enable_postgresql_controls()


def _backfill_platform_state() -> None:
    connection = op.get_bind()
    organizations = [row[0] for row in connection.execute(sa.text("SELECT id FROM organizations"))]
    now = datetime.now(UTC)
    defaults = {
        "max_concurrent_cpu_jobs": 3,
        "max_concurrent_expensive_jobs": 1,
        "max_artifact_bytes": 10 * 1024 * 1024 * 1024,
        "max_artifact_count": 10_000,
        "max_corpus_sentences": 1_000_000,
        "max_generation_accepted_sentences": 100,
        "max_generation_iterations": 500,
        "max_activity_deadline_seconds": 300.0,
        "max_provider_input_tokens": 1_000_000,
        "max_provider_output_tokens": 100_000,
        "max_provider_cost_microusd": 10_000_000,
        "max_rl_steps": 10_000,
        "max_rl_tokens": 10_000_000,
        "max_checkpoint_bytes": 100 * 1024 * 1024,
        "updated_at": now,
    }
    active_rows = list(
        connection.execute(
            sa.text(
                "SELECT id, organization_id, kind FROM runs "
                "WHERE state IN ('QUEUED', 'PROVISIONING', 'RUNNING', 'CANCELLING')"
            )
        )
    )
    counters: dict[UUID, tuple[int, int]] = dict.fromkeys(organizations, (0, 0))
    reservations: list[dict[str, object]] = []
    for run_id, organization_id, kind in active_rows:
        if kind in _CPU_KINDS:
            cpu, expensive = counters[organization_id]
            counters[organization_id] = cpu + 1, expensive
            quota_class = "CPU"
        elif kind in _EXPENSIVE_KINDS:
            cpu, expensive = counters[organization_id]
            counters[organization_id] = cpu, expensive + 1
            quota_class = "EXPENSIVE"
        else:
            raise RuntimeError(f"unclassified persisted run kind: {kind}")
        reservations.append(
            {
                "id": uuid4(),
                "organization_id": organization_id,
                "run_id": run_id,
                "quota_class": quota_class,
                "amount": 1,
                "state": "ACTIVE",
                "expires_at": now + timedelta(hours=24, minutes=5),
                "released_at": None,
                "created_at": now,
            }
        )
    artifact_usage = {
        row[0]: (int(row[1]), int(row[2]))
        for row in connection.execute(
            sa.text(
                "SELECT organization_id, COALESCE(SUM(size_bytes), 0), COUNT(*) "
                "FROM artifacts WHERE state <> 'DELETED' GROUP BY organization_id"
            )
        )
    }
    sentence_usage = {
        row[0]: int(row[1])
        for row in connection.execute(
            sa.text("SELECT organization_id, COUNT(*) FROM sentences GROUP BY organization_id")
        )
    }
    policies = sa.table("quota_policies", sa.column("organization_id"), *map(sa.column, defaults))
    usages = sa.table(
        "quota_usages",
        sa.column("organization_id"),
        sa.column("active_cpu_jobs"),
        sa.column("active_expensive_jobs"),
        sa.column("artifact_bytes"),
        sa.column("artifact_count"),
        sa.column("corpus_sentences"),
        sa.column("updated_at"),
    )
    heads = sa.table(
        "audit_heads",
        sa.column("organization_id"),
        sa.column("last_sequence"),
        sa.column("last_hash"),
        sa.column("updated_at"),
    )
    if organizations:
        op.bulk_insert(
            policies,
            [{"organization_id": organization, **defaults} for organization in organizations],
        )
        op.bulk_insert(
            usages,
            [
                {
                    "organization_id": organization,
                    "active_cpu_jobs": counters[organization][0],
                    "active_expensive_jobs": counters[organization][1],
                    "artifact_bytes": artifact_usage.get(organization, (0, 0))[0],
                    "artifact_count": artifact_usage.get(organization, (0, 0))[1],
                    "corpus_sentences": sentence_usage.get(organization, 0),
                    "updated_at": now,
                }
                for organization in organizations
            ],
        )
        op.bulk_insert(
            heads,
            [
                {
                    "organization_id": organization,
                    "last_sequence": 0,
                    "last_hash": _GENESIS_HASH,
                    "updated_at": now,
                }
                for organization in organizations
            ],
        )
    if reservations:
        reservations_table = sa.table(
            "quota_reservations",
            *map(sa.column, reservations[0]),
        )
        op.bulk_insert(reservations_table, reservations)


def _enable_postgresql_controls() -> None:
    _ensure_database_group_roles()
    op.execute(
        """
        CREATE FUNCTION corpuskit_is_current_member(target_organization_id uuid)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = off
        AS $$
            SELECT EXISTS (
                SELECT 1
                FROM public.memberships AS membership
                JOIN public.users AS app_user ON app_user.id = membership.user_id
                WHERE membership.organization_id = target_organization_id
                  AND app_user.oidc_subject = current_setting('corpuskit.actor_id', true)
            )
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION corpuskit_is_current_member(uuid) FROM PUBLIC")
    for role in (
        "corpuskit_api",
        "corpuskit_dispatcher",
        "corpuskit_worker",
        "corpuskit_adoption",
        "corpuskit_maintenance",
        "corpuskit_platform",
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION corpuskit_is_current_member(uuid) TO {role}")
    op.execute(
        """
        CREATE FUNCTION corpuskit_reject_audit_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit evidence is immutable' USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER corpuskit_audit_events_immutable
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION corpuskit_reject_audit_mutation()
        """
    )
    _enable_users_rls()
    _enable_memberships_rls()
    _enable_org_table_rls("organizations", organization_column="id")
    for table in (
        "projects",
        "corpora",
        "corpus_versions",
        "sentences",
        "runs",
        "run_events",
        "quota_usages",
    ):
        _enable_org_table_rls(table)
    _enable_org_table_rls("quota_reservations", allow_global="maintenance")
    _enable_audit_head_rls()
    _enable_org_table_rls(
        "outbox_messages",
        allow_global="dispatcher",
        allow_global_update=True,
    )
    _enable_org_table_rls("artifacts", allow_global="maintenance")
    _enable_quota_policy_rls()
    _enable_audit_event_rls()


def _enable_users_rls() -> None:
    table = "users"
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    visible = (
        "((current_setting('corpuskit.identity', true) = 'user' "
        "AND pg_has_role(session_user, 'corpuskit_api', 'member') "
        "AND oidc_subject = current_setting('corpuskit.actor_id', true)) "
        "OR (current_setting('corpuskit.identity', true) = 'platform' "
        "AND pg_has_role(session_user, 'corpuskit_platform', 'member')))"
    )
    _policy(table, "select", visible)
    _policy(table, "insert", None, _platform_scope("NULL"))
    _policy(table, "update", _platform_scope("NULL"), _platform_scope("NULL"))


def _enable_memberships_rls() -> None:
    table = "memberships"
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    org_match = _organization_match("organization_id")
    own = (
        "(current_setting('corpuskit.identity', true) = 'user' "
        "AND pg_has_role(session_user, 'corpuskit_api', 'member') "
        "AND user_id IN (SELECT id FROM users "
        "WHERE oidc_subject = current_setting('corpuskit.actor_id', true)))"
    )
    service = _service_scope()
    visible = f"({org_match} AND ({own} OR {service}))"
    _policy(table, "select", visible)
    _policy(table, "insert", None, f"({org_match} AND {_platform_scope('organization_id')})")
    _policy(
        table,
        "update",
        f"({org_match} AND {_platform_scope('organization_id')})",
        f"({org_match} AND {_platform_scope('organization_id')})",
    )


def _enable_org_table_rls(
    table: str,
    *,
    organization_column: str = "organization_id",
    allow_global: str | None = None,
    allow_global_update: bool = False,
) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    column = f'"{table}"."{organization_column}"'
    visible = _tenant_scope(column)
    if allow_global is not None:
        visible = f"({visible} OR {_global_service_scope(allow_global)})"
    _policy(table, "select", visible)
    _policy(table, "insert", None, _tenant_scope(column))
    update_check = _tenant_scope(column)
    if allow_global is not None and allow_global_update:
        update_check = f"({update_check} OR {_global_service_scope(allow_global)})"
    _policy(table, "update", visible, update_check)
    _policy(table, "delete", _tenant_scope(column))


def _enable_quota_policy_rls() -> None:
    table = "quota_policies"
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    column = '"quota_policies"."organization_id"'
    _policy(table, "select", _tenant_scope(column))
    platform = _platform_scope(column)
    _policy(table, "insert", None, platform)
    _policy(table, "update", platform, platform)


def _enable_audit_event_rls() -> None:
    table = "audit_events"
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    tenant = _tenant_scope('"audit_events"."organization_id"')
    _policy(table, "select", tenant)
    _policy(table, "insert", None, tenant)


def _enable_audit_head_rls() -> None:
    table = "audit_heads"
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    tenant = _tenant_scope('"audit_heads"."organization_id"')
    _policy(table, "select", tenant)
    _policy(table, "insert", None, _platform_scope('"audit_heads"."organization_id"'))
    _policy(table, "update", tenant, tenant)


def _policy(
    table: str,
    operation: str,
    using: str | None,
    check: str | None = None,
) -> None:
    fragments = [f'CREATE POLICY "ck_{table}_{operation}" ON "{table}" FOR {operation.upper()}']
    if using is not None:
        fragments.append(f"USING ({using})")
    if check is not None:
        fragments.append(f"WITH CHECK ({check})")
    op.execute(" ".join(fragments))


def _organization_match(column: str) -> str:
    return f"{column} = NULLIF(current_setting('corpuskit.organization_id', true), '')::uuid"


def _tenant_scope(column: str) -> str:
    organization_match = _organization_match(column)
    user_scope = (
        "(current_setting('corpuskit.identity', true) = 'user' "
        "AND pg_has_role(session_user, 'corpuskit_api', 'member') "
        f"AND corpuskit_is_current_member({column}))"
    )
    return f"({organization_match} AND ({user_scope} OR {_service_scope()}))"


def _service_scope() -> str:
    pairs = (
        ("worker", "corpuskit_worker"),
        ("adoption", "corpuskit_adoption"),
        ("maintenance", "corpuskit_maintenance"),
        ("platform", "corpuskit_platform"),
    )
    return (
        "("
        + " OR ".join(
            "(current_setting('corpuskit.identity', true) = "
            f"'{identity}' AND pg_has_role(session_user, '{role}', 'member'))"
            for identity, role in pairs
        )
        + ")"
    )


def _platform_scope(column: str) -> str:
    org = "TRUE" if column == "NULL" else _organization_match(column)
    return (
        f"({org} AND current_setting('corpuskit.identity', true) = 'platform' "
        "AND pg_has_role(session_user, 'corpuskit_platform', 'member'))"
    )


def _global_service_scope(identity: str) -> str:
    return (
        f"(current_setting('corpuskit.identity', true) = '{identity}' "
        f"AND pg_has_role(session_user, 'corpuskit_{identity}', 'member'))"
    )


def _ensure_database_group_roles() -> None:
    for role in (
        "corpuskit_api",
        "corpuskit_dispatcher",
        "corpuskit_worker",
        "corpuskit_adoption",
        "corpuskit_maintenance",
        "corpuskit_platform",
    ):
        op.execute(
            f"""
            DO $corpuskit$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role}
                        NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
                ELSIF EXISTS (
                    SELECT 1
                    FROM pg_roles
                    WHERE rolname = '{role}'
                      AND (
                          rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                          OR rolreplication OR rolbypassrls
                      )
                ) THEN
                    RAISE EXCEPTION 'CorpusKit policy role {role} has unsafe attributes';
                END IF;
            END
            $corpuskit$
            """  # noqa: S608
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in (
            "audit_events",
            "quota_policies",
            "audit_heads",
            "quota_reservations",
            "quota_usages",
            "artifacts",
            "outbox_messages",
            "run_events",
            "runs",
            "sentences",
            "corpus_versions",
            "corpora",
            "projects",
            "organizations",
            "memberships",
            "users",
        ):
            op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
            for operation in ("select", "insert", "update", "delete"):
                op.execute(f'DROP POLICY IF EXISTS "ck_{table}_{operation}" ON "{table}"')
        op.execute("DROP TRIGGER corpuskit_audit_events_immutable ON audit_events")
        op.execute("DROP FUNCTION corpuskit_reject_audit_mutation()")
        op.execute("DROP FUNCTION IF EXISTS corpuskit_is_current_member(uuid)")
    op.drop_table("audit_events")
    op.drop_table("audit_heads")
    op.drop_table("quota_reservations")
    op.drop_table("quota_usages")
    op.drop_table("quota_policies")
