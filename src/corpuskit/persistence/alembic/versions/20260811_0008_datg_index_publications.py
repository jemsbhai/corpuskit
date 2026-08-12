"""Add tenant-scoped authorization for parent-published DATG indexes.

Revision ID: 0008_datg_index_publications
Revises: 0007_project_deletion
Created: 2026-08-12 03:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0008_datg_index_publications"
down_revision: str | None = "0007_project_deletion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Uuid[UUID]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "datg_index_publications",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("project_id", _id(), nullable=False),
        sa.Column("build_run_id", _id(), nullable=False),
        sa.Column("created_by", _id(), nullable=False),
        sa.Column("cache_key_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("runtime_id", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("vocabulary_size", sa.Integer(), nullable=False),
        sa.Column("indexed_token_count", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(cache_key_sha256) = 64",
            name=op.f("ck_datg_index_publications_cache_key_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name=op.f("ck_datg_index_publications_content_sha256_length"),
        ),
        sa.CheckConstraint(
            "vocabulary_size > 0",
            name=op.f("ck_datg_index_publications_positive_vocabulary_size"),
        ),
        sa.CheckConstraint(
            "indexed_token_count >= 0",
            name=op.f("ck_datg_index_publications_nonnegative_indexed_token_count"),
        ),
        sa.CheckConstraint(
            "indexed_token_count <= vocabulary_size",
            name=op.f("ck_datg_index_publications_indexed_token_count_bounded"),
        ),
        sa.CheckConstraint(
            "size_bytes > 0",
            name=op.f("ck_datg_index_publications_positive_size_bytes"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_datg_index_publications_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_datg_index_publications_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["build_run_id"],
            ["runs.id"],
            name=op.f("fk_datg_index_publications_build_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_datg_index_publications_created_by_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datg_index_publications")),
        sa.UniqueConstraint(
            "build_run_id",
            name=op.f("uq_datg_index_publications_build_run_id"),
        ),
        sa.UniqueConstraint(
            "organization_id",
            "project_id",
            "cache_key_sha256",
            name="uq_datg_index_publications_project_cache_key",
        ),
    )
    op.create_index(
        op.f("ix_datg_index_publications_organization_id"),
        "datg_index_publications",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_datg_index_publications_project_id"),
        "datg_index_publications",
        ["project_id"],
    )
    op.create_index(
        "ix_datg_index_publications_org_project_created",
        "datg_index_publications",
        ["organization_id", "project_id", "created_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        _enable_postgresql_controls()


def _enable_postgresql_controls() -> None:
    table = "datg_index_publications"
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
    adoption = (
        f"({tenant} AND current_setting('corpuskit.identity', true) = 'adoption' "  # noqa: S608 -- migration-owned SQL fragment
        "AND pg_has_role(session_user, 'corpuskit_adoption', 'member') "
        "AND EXISTS (SELECT 1 FROM runs "
        "WHERE runs.id = datg_index_publications.build_run_id "
        "AND runs.organization_id = datg_index_publications.organization_id "
        "AND runs.project_id = datg_index_publications.project_id "
        "AND runs.created_by = datg_index_publications.created_by "
        "AND runs.kind = 'BUILD_DATG_INDEX' "
        "AND runs.state = 'RUNNING'))"
    )
    maintenance = (
        f"({tenant} AND current_setting('corpuskit.identity', true) = 'maintenance' "
        "AND pg_has_role(session_user, 'corpuskit_maintenance', 'member'))"
    )
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(f'CREATE POLICY "ck_{table}_select" ON "{table}" FOR SELECT USING ({visible})')
    op.execute(f'CREATE POLICY "ck_{table}_insert" ON "{table}" FOR INSERT WITH CHECK ({adoption})')
    op.execute(f'CREATE POLICY "ck_{table}_delete" ON "{table}" FOR DELETE USING ({maintenance})')
    op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
    op.execute(f"GRANT SELECT ON {table} TO corpuskit_api, corpuskit_worker, corpuskit_platform")
    op.execute(f"GRANT SELECT, INSERT ON {table} TO corpuskit_adoption")
    op.execute(f"GRANT SELECT, DELETE ON {table} TO corpuskit_maintenance")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        table = "datg_index_publications"
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
        for operation in ("select", "insert", "delete"):
            op.execute(f'DROP POLICY IF EXISTS "ck_{table}_{operation}" ON "{table}"')
    op.drop_table("datg_index_publications")
