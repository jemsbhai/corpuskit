"""Create the multi-tenant corpus and run schema.

Revision ID: 0001_tenancy_baseline
Revises: None
Created: 2026-08-11 00:00:00+00:00
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0001_tenancy_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Uuid[UUID]:
    return sa.Uuid(as_uuid=True)


def _created_at() -> sa.Column[datetime]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", _id(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_organizations"),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
    )
    op.create_table(
        "users",
        sa.Column("id", _id(), nullable=False),
        sa.Column("oidc_subject", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("oidc_subject", name="uq_users_oidc_subject"),
    )
    op.create_table(
        "memberships",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("user_id", _id(), nullable=False),
        sa.Column(
            "role",
            sa.Enum("OWNER", "ADMIN", "EDITOR", "VIEWER", name="role", native_enum=False),
            nullable=False,
        ),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_memberships_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_memberships_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memberships"),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_memberships_organization_id"),
    )
    op.create_index("ix_memberships_organization_id", "memberships", ["organization_id"])
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_table(
        "projects",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("created_by", _id(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_projects_created_by_users"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_projects_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.UniqueConstraint("organization_id", "name", name="uq_projects_organization_id"),
    )
    op.create_index("ix_projects_organization_id", "projects", ["organization_id"])
    op.create_table(
        "corpora",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("project_id", _id(), nullable=False),
        sa.Column("created_by", _id(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_corpora_created_by_users"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_corpora_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_corpora_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_corpora"),
        sa.UniqueConstraint("project_id", "name", name="uq_corpora_project_id"),
    )
    op.create_index("ix_corpora_organization_id", "corpora", ["organization_id"])
    op.create_index("ix_corpora_project_id", "corpora", ["project_id"])
    op.create_table(
        "corpus_versions",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("corpus_id", _id(), nullable=False),
        sa.Column("parent_version_id", _id(), nullable=True),
        sa.Column("created_by", _id(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=64), nullable=False),
        sa.Column("sentence_count", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("corpusgen_version", sa.String(length=32), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "sentence_count > 0", name=op.f("ck_corpus_versions_positive_sentence_count")
        ),
        sa.CheckConstraint("version_number > 0", name=op.f("ck_corpus_versions_positive_version")),
        sa.ForeignKeyConstraint(
            ["corpus_id"],
            ["corpora.id"],
            name="fk_corpus_versions_corpus_id_corpora",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_corpus_versions_created_by_users"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_corpus_versions_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_version_id"],
            ["corpus_versions.id"],
            name="fk_corpus_versions_parent_version_id_corpus_versions",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_corpus_versions"),
        sa.UniqueConstraint(
            "corpus_id",
            "content_sha256",
            name="uq_corpus_versions_corpus_content_sha256",
        ),
        sa.UniqueConstraint(
            "corpus_id",
            "version_number",
            name="uq_corpus_versions_corpus_version_number",
        ),
    )
    op.create_index("ix_corpus_versions_organization_id", "corpus_versions", ["organization_id"])
    op.create_index("ix_corpus_versions_corpus_id", "corpus_versions", ["corpus_id"])
    op.create_table(
        "runs",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("project_id", _id(), nullable=False),
        sa.Column("corpus_version_id", _id(), nullable=True),
        sa.Column("created_by", _id(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "PHONEMIZE",
                "EVALUATE",
                "DISTRIBUTION",
                "TRAJECTORY",
                "ERROR_RATES",
                "PERPLEXITY",
                "SELECT",
                "GENERATE_REPOSITORY",
                "GENERATE_LLM",
                "GENERATE_LOCAL",
                "TRAIN_PHON_RL",
                "EXPORT",
                name="runkind",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "DRAFT",
                "QUEUED",
                "PROVISIONING",
                "RUNNING",
                "CANCELLING",
                "CANCELLED",
                "SUCCEEDED",
                "FAILED",
                name="runstate",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("spec_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_summary", sa.JSON(), nullable=True),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["corpus_version_id"],
            ["corpus_versions.id"],
            name="fk_runs_corpus_version_id_corpus_versions",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_runs_created_by_users"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_runs_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_runs_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
        sa.UniqueConstraint("organization_id", "idempotency_key", name="uq_runs_organization_id"),
    )
    op.create_index("ix_runs_organization_id", "runs", ["organization_id"])
    op.create_index("ix_runs_project_id", "runs", ["project_id"])
    op.create_index("ix_runs_org_state_created", "runs", ["organization_id", "state", "created_at"])
    op.create_table(
        "sentences",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("corpus_version_id", _id(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("phonemes", sa.JSON(), nullable=True),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_sentences_nonnegative_ordinal")),
        sa.ForeignKeyConstraint(
            ["corpus_version_id"],
            ["corpus_versions.id"],
            name="fk_sentences_corpus_version_id_corpus_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_sentences_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sentences"),
        sa.UniqueConstraint("corpus_version_id", "ordinal", name="uq_sentences_corpus_version_id"),
    )
    op.create_index("ix_sentences_organization_id", "sentences", ["organization_id"])
    op.create_index("ix_sentences_corpus_version_id", "sentences", ["corpus_version_id"])
    op.create_table(
        "artifacts",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("project_id", _id(), nullable=False),
        sa.Column("run_id", _id(), nullable=True),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("media_type", sa.String(length=160), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_artifacts_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_artifacts_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name="fk_artifacts_run_id_runs"),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
        sa.UniqueConstraint(
            "organization_id", "sha256", "kind", name="uq_artifacts_organization_id"
        ),
        sa.UniqueConstraint("storage_key", name="uq_artifacts_storage_key"),
    )
    op.create_index("ix_artifacts_organization_id", "artifacts", ["organization_id"])
    op.create_index("ix_artifacts_project_id", "artifacts", ["project_id"])
    op.create_table(
        "run_events",
        sa.Column("id", _id(), nullable=False),
        sa.Column("organization_id", _id(), nullable=False),
        sa.Column("run_id", _id(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_run_events_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name="fk_run_events_run_id_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_events"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_run_id"),
    )
    op.create_index("ix_run_events_organization_id", "run_events", ["organization_id"])
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])


def downgrade() -> None:
    op.drop_table("run_events")
    op.drop_table("artifacts")
    op.drop_table("sentences")
    op.drop_table("runs")
    op.drop_table("corpus_versions")
    op.drop_table("corpora")
    op.drop_table("projects")
    op.drop_table("memberships")
    op.drop_table("users")
    op.drop_table("organizations")
