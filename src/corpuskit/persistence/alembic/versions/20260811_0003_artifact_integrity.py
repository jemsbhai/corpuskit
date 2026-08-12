"""Harden immutable artifact metadata and retention lifecycle.

Revision ID: 0003_artifact_integrity
Revises: 0002_durable_job_outbox
Created: 2026-08-11 17:00:00+00:00
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0003_artifact_integrity"
down_revision: str | None = "0002_durable_job_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_IDENTITY_COLLISION_ERROR = (
    "Cannot downgrade 0003_artifact_integrity to 0002_durable_job_outbox: "
    "artifact rows cannot be represented by the legacy "
    "(organization_id, sha256, kind) uniqueness constraint. No schema changes were applied; "
    "keep revision 0003 or later and use an approved data-preserving remediation or restore."
)


def _id() -> sa.Uuid[UUID]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("created_by", _id(), nullable=True))
        batch.add_column(
            sa.Column("scope_key", sa.String(length=36), server_default="project", nullable=False)
        )
        batch.add_column(
            sa.Column(
                "filename", sa.String(length=255), server_default="artifact.bin", nullable=False
            )
        )
        batch.add_column(
            sa.Column(
                "state",
                sa.Enum(
                    "ACTIVE",
                    "TOMBSTONED",
                    "DELETED",
                    name="artifactstate",
                    native_enum=False,
                ),
                server_default="ACTIVE",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

    op.execute(
        sa.text(
            "UPDATE artifacts SET created_by = "
            "(SELECT created_by FROM projects WHERE projects.id = artifacts.project_id)"
        )
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            sa.text("UPDATE artifacts SET retention_until = created_at + INTERVAL '30 days'")
        )
    else:
        op.execute(
            sa.text("UPDATE artifacts SET retention_until = datetime(created_at, '+30 days')")
        )

    with op.batch_alter_table("artifacts") as batch:
        batch.alter_column("created_by", nullable=False)
        batch.alter_column("retention_until", nullable=False)
        batch.alter_column("scope_key", server_default=None)
        batch.alter_column("filename", server_default=None)
        batch.alter_column("state", server_default=None)
        batch.create_foreign_key(
            "fk_artifacts_created_by_users",
            "users",
            ["created_by"],
            ["id"],
        )
        batch.drop_constraint("uq_artifacts_organization_id", type_="unique")
        batch.create_unique_constraint(
            "uq_artifacts_scope_digest",
            ["organization_id", "project_id", "scope_key", "kind", "sha256"],
        )
        batch.create_check_constraint(op.f("ck_artifacts_positive_size"), "size_bytes > 0")
        batch.create_index("ix_artifacts_run_id", ["run_id"])
        batch.create_index("ix_artifacts_retention", ["state", "retention_until"])


def downgrade() -> None:
    collision_exists = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM artifacts "
                "GROUP BY organization_id, sha256, kind "
                "HAVING COUNT(*) > 1"
                ")"
            )
        )
        .scalar_one()
    )
    if collision_exists:
        raise RuntimeError(_LEGACY_IDENTITY_COLLISION_ERROR)

    with op.batch_alter_table("artifacts") as batch:
        batch.drop_index("ix_artifacts_retention")
        batch.drop_index("ix_artifacts_run_id")
        batch.drop_constraint(op.f("ck_artifacts_positive_size"), type_="check")
        batch.drop_constraint("uq_artifacts_scope_digest", type_="unique")
        batch.create_unique_constraint(
            "uq_artifacts_organization_id",
            ["organization_id", "sha256", "kind"],
        )
        batch.drop_constraint("fk_artifacts_created_by_users", type_="foreignkey")
        batch.drop_column("deleted_at")
        batch.drop_column("tombstoned_at")
        batch.drop_column("retention_until")
        batch.drop_column("state")
        batch.drop_column("filename")
        batch.drop_column("scope_key")
        batch.drop_column("created_by")
