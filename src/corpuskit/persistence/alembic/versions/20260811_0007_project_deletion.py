"""Add retention-safe project deletion lifecycle state.

Revision ID: 0007_project_deletion
Revises: 0006_maintenance_cursors
Created: 2026-08-11 23:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_project_deletion"
down_revision: str | None = "0006_maintenance_cursors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    lifecycle = sa.Enum(
        "ACTIVE",
        "DELETION_PENDING",
        name="projectlifecycle",
        native_enum=False,
    )
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column(
                "lifecycle_state",
                lifecycle,
                server_default="ACTIVE",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("deletion_retention_until", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("deletion_corpus_sentences", sa.BigInteger(), nullable=True))
        batch.create_check_constraint(
            op.f("ck_projects_lifecycle_consistent"),
            "(lifecycle_state = 'ACTIVE' AND deletion_requested_at IS NULL "
            "AND deletion_retention_until IS NULL AND deletion_corpus_sentences IS NULL) "
            "OR (lifecycle_state = 'DELETION_PENDING' AND deletion_requested_at IS NOT NULL "
            "AND deletion_retention_until IS NOT NULL "
            "AND deletion_retention_until >= deletion_requested_at "
            "AND deletion_corpus_sentences IS NOT NULL)",
        )
        batch.create_check_constraint(
            op.f("ck_projects_nonnegative_deletion_corpus_sentences"),
            "deletion_corpus_sentences IS NULL OR deletion_corpus_sentences >= 0",
        )
        batch.create_index(
            "ix_projects_lifecycle_retention",
            ["lifecycle_state", "deletion_retention_until"],
            unique=False,
        )

    if op.get_bind().dialect.name == "postgresql":
        maintenance = (
            "current_setting('corpuskit.identity', true) = 'maintenance' "
            "AND pg_has_role(session_user, 'corpuskit_maintenance', 'member')"
        )
        pending_maintenance = f"({maintenance}) AND lifecycle_state = 'DELETION_PENDING'"
        tenant = (
            "organization_id = NULLIF(current_setting('corpuskit.organization_id', true), '')::uuid"
        )
        op.execute(
            'CREATE POLICY "ck_projects_maintenance_global_select" ON "projects" '
            f"FOR SELECT USING ({pending_maintenance})"
        )
        for table in ("run_execution_facts", "run_replays"):
            op.execute(
                f'CREATE POLICY "ck_{table}_maintenance_delete" ON "{table}" '
                f"FOR DELETE USING ({tenant} AND {maintenance})"
            )
        op.execute("GRANT UPDATE ON projects TO corpuskit_api")
        op.execute(
            "GRANT SELECT, DELETE ON projects, corpora, corpus_versions, sentences, runs, "
            "run_events, outbox_messages, artifacts, quota_reservations, "
            "run_execution_facts, run_replays TO corpuskit_maintenance"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in ("run_execution_facts", "run_replays"):
            op.execute(f'DROP POLICY IF EXISTS "ck_{table}_maintenance_delete" ON "{table}"')
        op.execute('DROP POLICY IF EXISTS "ck_projects_maintenance_global_select" ON "projects"')
    with op.batch_alter_table("projects") as batch:
        batch.drop_index("ix_projects_lifecycle_retention")
        batch.drop_constraint(op.f("ck_projects_nonnegative_deletion_corpus_sentences"))
        batch.drop_constraint(op.f("ck_projects_lifecycle_consistent"))
        batch.drop_column("deletion_corpus_sentences")
        batch.drop_column("deletion_retention_until")
        batch.drop_column("deletion_requested_at")
        batch.drop_column("lifecycle_state")
