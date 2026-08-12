"""Add trusted execution facts and durable replay lineage.

Revision ID: 0005_reproducibility
Revises: 0004_tenant_controls
Created: 2026-08-11 22:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0005_reproducibility"
down_revision: str | None = "0004_tenant_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Uuid[UUID]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "run_execution_facts",
        sa.Column("run_id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("project_id", _id(), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("facts_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_digests", sa.JSON(), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("manifest_artifact_id", _id(), nullable=True),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(facts_sha256) = 64",
            name=op.f("ck_run_execution_facts_facts_sha256_length"),
        ),
        sa.CheckConstraint(
            "manifest_sha256 IS NULL OR length(manifest_sha256) = 64",
            name=op.f("ck_run_execution_facts_manifest_sha256_length"),
        ),
        sa.CheckConstraint(
            "(manifest_artifact_id IS NULL AND manifest_sha256 IS NULL AND finalized_at IS NULL) "
            "OR (manifest_artifact_id IS NOT NULL AND manifest_sha256 IS NOT NULL "
            "AND finalized_at IS NOT NULL)",
            name=op.f("ck_run_execution_facts_manifest_completion_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["manifest_artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_run_execution_facts_manifest_artifact_id_artifacts"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_run_execution_facts_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_run_execution_facts_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_execution_facts_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_run_execution_facts")),
        sa.UniqueConstraint(
            "manifest_artifact_id",
            name=op.f("uq_run_execution_facts_manifest_artifact_id"),
        ),
    )
    op.create_index(
        op.f("ix_run_execution_facts_organization_id"),
        "run_execution_facts",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_run_execution_facts_project_id"),
        "run_execution_facts",
        ["project_id"],
    )
    op.create_table(
        "run_replays",
        sa.Column("replay_run_id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("project_id", _id(), nullable=False),
        sa.Column("source_run_id", _id(), nullable=False),
        sa.Column("source_manifest_artifact_id", _id(), nullable=False),
        sa.Column("expected_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("observed_manifest_artifact_id", _id(), nullable=True),
        sa.Column(
            "classification",
            sa.Enum(
                "EXACT",
                "BEST_EFFORT",
                "NONREPRODUCIBLE",
                name="determinismclass",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "verdict",
            sa.Enum(
                "EXACT_MATCH",
                "BEST_EFFORT_MATCH",
                "BEST_EFFORT_DIVERGENCE",
                "MISMATCH",
                "NONREPRODUCIBLE",
                name="replayverdict",
                native_enum=False,
            ),
            nullable=True,
        ),
        sa.Column("comparison", sa.JSON(), nullable=True),
        sa.Column("created_by", _id(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "length(expected_manifest_sha256) = 64",
            name=op.f("ck_run_replays_expected_sha256_length"),
        ),
        sa.CheckConstraint(
            "(observed_manifest_artifact_id IS NULL AND verdict IS NULL "
            "AND comparison IS NULL AND completed_at IS NULL) "
            "OR (observed_manifest_artifact_id IS NOT NULL AND verdict IS NOT NULL "
            "AND comparison IS NOT NULL AND completed_at IS NOT NULL)",
            name=op.f("ck_run_replays_comparison_completion_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_run_replays_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["observed_manifest_artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_run_replays_observed_manifest_artifact_id_artifacts"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_run_replays_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_run_replays_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["replay_run_id"],
            ["runs.id"],
            name=op.f("fk_run_replays_replay_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_manifest_artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_run_replays_source_manifest_artifact_id_artifacts"),
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["runs.id"],
            name=op.f("fk_run_replays_source_run_id_runs"),
        ),
        sa.PrimaryKeyConstraint("replay_run_id", name=op.f("pk_run_replays")),
        sa.UniqueConstraint(
            "observed_manifest_artifact_id",
            name=op.f("uq_run_replays_observed_manifest_artifact_id"),
        ),
    )
    op.create_index(op.f("ix_run_replays_organization_id"), "run_replays", ["organization_id"])
    op.create_index(op.f("ix_run_replays_project_id"), "run_replays", ["project_id"])
    op.create_index(op.f("ix_run_replays_source_run_id"), "run_replays", ["source_run_id"])
    op.create_index(
        op.f("ix_run_replays_source_manifest_artifact_id"),
        "run_replays",
        ["source_manifest_artifact_id"],
    )
    op.create_index(
        "ix_run_replays_org_source",
        "run_replays",
        ["organization_id", "source_run_id", "created_at"],
    )

    if op.get_bind().dialect.name == "postgresql":
        _enable_postgresql_controls()


def _enable_postgresql_controls() -> None:
    _create_immutability_triggers()
    tenant = (
        "organization_id = NULLIF(current_setting('corpuskit.organization_id', true), '')::uuid"
    )
    user_member = (
        "current_setting('corpuskit.identity', true) = 'user' "
        "AND pg_has_role(session_user, 'corpuskit_api', 'member') "
        "AND corpuskit_is_current_member(organization_id)"
    )
    service = " OR ".join(
        "(current_setting('corpuskit.identity', true) = "
        f"'{identity}' AND pg_has_role(session_user, 'corpuskit_{identity}', 'member'))"
        for identity in ("worker", "adoption", "maintenance", "platform")
    )
    visible = f"({tenant} AND (({user_member}) OR ({service})))"
    worker_write = (
        f"({tenant} AND current_setting('corpuskit.identity', true) = 'worker' "
        "AND pg_has_role(session_user, 'corpuskit_worker', 'member'))"
    )
    finalizer_write = (
        f"({tenant} AND current_setting('corpuskit.identity', true) = 'adoption' "
        "AND pg_has_role(session_user, 'corpuskit_adoption', 'member'))"
    )
    replay_insert = f"({tenant} AND ({user_member}))"

    for table in ("run_execution_facts", "run_replays"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY "ck_{table}_select" ON "{table}" FOR SELECT USING ({visible})')
    op.execute(
        'CREATE POLICY "ck_run_execution_facts_insert" ON "run_execution_facts" '
        f"FOR INSERT WITH CHECK ({worker_write})"
    )
    op.execute(
        'CREATE POLICY "ck_run_execution_facts_update" ON "run_execution_facts" '
        f"FOR UPDATE USING ({finalizer_write}) WITH CHECK ({finalizer_write})"
    )
    op.execute(
        'CREATE POLICY "ck_run_replays_insert" ON "run_replays" '
        f"FOR INSERT WITH CHECK ({replay_insert})"
    )
    op.execute(
        'CREATE POLICY "ck_run_replays_update" ON "run_replays" '
        f"FOR UPDATE USING ({finalizer_write}) WITH CHECK ({finalizer_write})"
    )

    op.execute("REVOKE ALL ON run_execution_facts, run_replays FROM PUBLIC")
    op.execute("GRANT SELECT ON run_execution_facts TO corpuskit_api")
    op.execute("GRANT SELECT, INSERT ON run_execution_facts TO corpuskit_worker")
    op.execute("GRANT SELECT, UPDATE ON run_execution_facts TO corpuskit_adoption")
    op.execute("GRANT SELECT, INSERT ON run_replays TO corpuskit_api")
    op.execute("GRANT SELECT, UPDATE ON run_replays TO corpuskit_worker, corpuskit_adoption")
    op.execute("GRANT SELECT ON run_execution_facts, run_replays TO corpuskit_maintenance")
    op.execute("GRANT SELECT ON run_execution_facts, run_replays TO corpuskit_platform")


def _create_immutability_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION corpuskit_guard_execution_fact_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.run_id IS DISTINCT FROM OLD.run_id
               OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.facts::jsonb IS DISTINCT FROM OLD.facts::jsonb
               OR NEW.facts_sha256 IS DISTINCT FROM OLD.facts_sha256
               OR NEW.input_digests::jsonb IS DISTINCT FROM OLD.input_digests::jsonb
               OR NEW.recorded_at IS DISTINCT FROM OLD.recorded_at THEN
                RAISE EXCEPTION 'execution facts are immutable' USING ERRCODE = '55000';
            END IF;
            IF OLD.manifest_artifact_id IS NOT NULL AND (
               NEW.manifest_artifact_id IS DISTINCT FROM OLD.manifest_artifact_id
               OR NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
               OR NEW.finalized_at IS DISTINCT FROM OLD.finalized_at) THEN
                RAISE EXCEPTION 'manifest binding is immutable' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER corpuskit_run_execution_facts_immutable
        BEFORE UPDATE ON run_execution_facts
        FOR EACH ROW EXECUTE FUNCTION corpuskit_guard_execution_fact_update()
        """
    )
    op.execute(
        """
        CREATE FUNCTION corpuskit_guard_replay_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.replay_run_id IS DISTINCT FROM OLD.replay_run_id
               OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.source_run_id IS DISTINCT FROM OLD.source_run_id
               OR NEW.source_manifest_artifact_id IS DISTINCT FROM OLD.source_manifest_artifact_id
               OR NEW.expected_manifest_sha256 IS DISTINCT FROM OLD.expected_manifest_sha256
               OR NEW.classification IS DISTINCT FROM OLD.classification
               OR NEW.created_by IS DISTINCT FROM OLD.created_by
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'replay lineage is immutable' USING ERRCODE = '55000';
            END IF;
            IF OLD.completed_at IS NOT NULL AND (
               NEW.observed_manifest_artifact_id IS DISTINCT FROM OLD.observed_manifest_artifact_id
               OR NEW.verdict IS DISTINCT FROM OLD.verdict
               OR NEW.comparison::jsonb IS DISTINCT FROM OLD.comparison::jsonb
               OR NEW.completed_at IS DISTINCT FROM OLD.completed_at) THEN
                RAISE EXCEPTION 'replay comparison is immutable' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER corpuskit_run_replays_immutable
        BEFORE UPDATE ON run_replays
        FOR EACH ROW EXECUTE FUNCTION corpuskit_guard_replay_update()
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in ("run_replays", "run_execution_facts"):
            op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
            for operation in ("select", "insert", "update", "delete"):
                op.execute(f'DROP POLICY IF EXISTS "ck_{table}_{operation}" ON "{table}"')
        op.execute("DROP TRIGGER corpuskit_run_replays_immutable ON run_replays")
        op.execute("DROP FUNCTION corpuskit_guard_replay_update()")
        op.execute("DROP TRIGGER corpuskit_run_execution_facts_immutable ON run_execution_facts")
        op.execute("DROP FUNCTION corpuskit_guard_execution_fact_update()")
    op.drop_table("run_replays")
    op.drop_table("run_execution_facts")
