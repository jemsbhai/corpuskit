"""Add centralized authenticated API rate-limit windows.

Revision ID: 0009_api_rate_limits
Revises: 0008_datg_index_publications
Created: 2026-08-12 04:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0009_api_rate_limits"
down_revision: str | None = "0008_datg_index_publications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Uuid[UUID]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "api_rate_limit_windows",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("subject_sha256", sa.String(length=64), nullable=False),
        sa.Column("route_sha256", sa.String(length=64), nullable=False),
        sa.Column("method", sa.String(length=7), nullable=False),
        sa.Column("window_epoch", sa.BigInteger(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(subject_sha256) = 64",
            name=op.f("ck_api_rate_limit_windows_subject_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(route_sha256) = 64",
            name=op.f("ck_api_rate_limit_windows_route_sha256_length"),
        ),
        sa.CheckConstraint(
            "request_count > 0",
            name=op.f("ck_api_rate_limit_windows_positive_request_count"),
        ),
        sa.CheckConstraint(
            "window_epoch >= 0",
            name=op.f("ck_api_rate_limit_windows_nonnegative_window_epoch"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_api_rate_limit_windows_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_rate_limit_windows")),
        sa.UniqueConstraint(
            "organization_id",
            "subject_sha256",
            "route_sha256",
            "method",
            "window_epoch",
            name="uq_api_rate_limit_windows_scope",
        ),
    )
    op.create_index(
        op.f("ix_api_rate_limit_windows_organization_id"),
        "api_rate_limit_windows",
        ["organization_id"],
    )
    op.create_index(
        "ix_api_rate_limit_windows_expiry",
        "api_rate_limit_windows",
        ["window_epoch", "id"],
    )
    if op.get_bind().dialect.name == "postgresql":
        _enable_postgresql_controls()


def _enable_postgresql_controls() -> None:
    table = "api_rate_limit_windows"
    tenant = (
        "organization_id = NULLIF(current_setting('corpuskit.organization_id', true), '')::uuid"
    )
    api_user = (
        f"({tenant} AND current_setting('corpuskit.identity', true) = 'user' "
        "AND pg_has_role(session_user, 'corpuskit_api', 'member') "
        "AND corpuskit_is_current_member(organization_id))"
    )
    maintenance = (
        "(current_setting('corpuskit.identity', true) = 'maintenance' "
        "AND pg_has_role(session_user, 'corpuskit_maintenance', 'member'))"
    )
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(f'CREATE POLICY "ck_{table}_api_select" ON "{table}" FOR SELECT USING ({api_user})')
    op.execute(
        f'CREATE POLICY "ck_{table}_api_insert" ON "{table}" FOR INSERT WITH CHECK ({api_user})'
    )
    op.execute(
        f'CREATE POLICY "ck_{table}_api_update" ON "{table}" '
        f"FOR UPDATE USING ({api_user}) WITH CHECK ({api_user})"
    )
    op.execute(
        f'CREATE POLICY "ck_{table}_maintenance_select" ON "{table}" '
        f"FOR SELECT USING ({maintenance})"
    )
    op.execute(
        f'CREATE POLICY "ck_{table}_maintenance_delete" ON "{table}" '
        f"FOR DELETE USING ({maintenance})"
    )
    op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO corpuskit_api")
    op.execute(f"GRANT SELECT, DELETE ON {table} TO corpuskit_maintenance")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        table = "api_rate_limit_windows"
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
        for policy in (
            "api_select",
            "api_insert",
            "api_update",
            "maintenance_select",
            "maintenance_delete",
        ):
            op.execute(f'DROP POLICY IF EXISTS "ck_{table}_{policy}" ON "{table}"')
    op.drop_table("api_rate_limit_windows")
