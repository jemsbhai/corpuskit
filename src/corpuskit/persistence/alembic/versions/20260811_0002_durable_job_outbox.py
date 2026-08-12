"""Add durable run lineage, event allocation, and transactional outbox.

Revision ID: 0002_durable_job_outbox
Revises: 0001_tenancy_baseline
Created: 2026-08-11 14:00:00+00:00
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0002_durable_job_outbox"
down_revision: str | None = "0001_tenancy_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Uuid[UUID]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("parent_run_id", _id(), nullable=True))
        batch.add_column(
            sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False)
        )
        batch.add_column(
            sa.Column("event_sequence", sa.Integer(), server_default=sa.text("1"), nullable=False)
        )
        batch.add_column(sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)))
        batch.create_foreign_key(
            "fk_runs_parent_run_id_runs",
            "runs",
            ["parent_run_id"],
            ["id"],
        )
        batch.create_check_constraint(op.f("ck_runs_positive_attempt"), "attempt > 0")
        batch.create_check_constraint(op.f("ck_runs_positive_event_sequence"), "event_sequence > 0")
        batch.create_index("ix_runs_parent_run_id", ["parent_run_id"])

    op.execute(
        sa.text(
            "UPDATE runs SET event_sequence = CASE "
            "WHEN COALESCE((SELECT MAX(sequence) FROM run_events "
            "WHERE run_events.run_id = runs.id), 1) > 0 "
            "THEN COALESCE((SELECT MAX(sequence) FROM run_events "
            "WHERE run_events.run_id = runs.id), 1) ELSE 1 END"
        )
    )
    with op.batch_alter_table("runs") as batch:
        batch.alter_column("attempt", server_default=None)
        batch.alter_column("event_sequence", server_default=None)

    op.create_table(
        "outbox_messages",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("run_id", _id(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "state",
            sa.Enum("PENDING", "CLAIMED", "PUBLISHED", name="outboxstate", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=80), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_outbox_messages_nonnegative_attempts")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_outbox_messages_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name="fk_outbox_messages_run_id_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_messages"),
    )
    op.create_index("ix_outbox_messages_organization_id", "outbox_messages", ["organization_id"])
    op.create_index("ix_outbox_messages_run_id", "outbox_messages", ["run_id"])
    op.create_index("ix_outbox_claim", "outbox_messages", ["state", "available_at", "created_at"])


def downgrade() -> None:
    op.drop_table("outbox_messages")
    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_parent_run_id")
        batch.drop_constraint(op.f("ck_runs_positive_event_sequence"), type_="check")
        batch.drop_constraint(op.f("ck_runs_positive_attempt"), type_="check")
        batch.drop_constraint("fk_runs_parent_run_id_runs", type_="foreignkey")
        batch.drop_column("cancellation_requested_at")
        batch.drop_column("event_sequence")
        batch.drop_column("attempt")
        batch.drop_column("parent_run_id")
