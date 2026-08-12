"""Add private durable cursor state for bounded maintenance scans.

Revision ID: 0006_maintenance_cursors
Revises: 0005_reproducibility
Created: 2026-08-11 23:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_maintenance_cursors"
down_revision: str | None = "0005_reproducibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_cursors",
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("backend_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("cursor", sa.String(length=512), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(backend_fingerprint) = 64",
            name=op.f("ck_maintenance_cursors_backend_fingerprint_length"),
        ),
        sa.PrimaryKeyConstraint(
            "operation",
            "backend_fingerprint",
            name=op.f("pk_maintenance_cursors"),
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        allowed = (
            "current_setting('corpuskit.identity', true) = 'maintenance' "
            "AND pg_has_role(session_user, 'corpuskit_maintenance', 'member')"
        )
        op.execute('ALTER TABLE "maintenance_cursors" ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE "maintenance_cursors" FORCE ROW LEVEL SECURITY')
        op.execute(
            'CREATE POLICY "ck_maintenance_cursors_all" ON "maintenance_cursors" '
            f"FOR ALL USING ({allowed}) WITH CHECK ({allowed})"
        )
        op.execute("REVOKE ALL ON maintenance_cursors FROM PUBLIC")
        op.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON maintenance_cursors TO corpuskit_maintenance"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE "maintenance_cursors" DISABLE ROW LEVEL SECURITY')
        op.execute('DROP POLICY IF EXISTS "ck_maintenance_cursors_all" ON "maintenance_cursors"')
    op.drop_table("maintenance_cursors")
