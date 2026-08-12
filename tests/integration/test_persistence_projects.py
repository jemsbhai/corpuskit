"""Real SQLAlchemy integration tests for tenant-scoped immutable corpora."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from corpuskit.domain.corpora import CorpusImportLimits, CorpusImportRequest, prepare_corpus
from corpuskit.domain.errors import ResourceConflictError, ResourceNotFoundError
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import Membership, Organization, Project, Role, Sentence, User
from corpuskit.services.projects import ProjectService


@pytest_asyncio.fixture
async def database() -> AsyncIterator[Database]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    database = Database("sqlite+aiosqlite:///:memory:", engine=engine)
    await database.create_schema()
    yield database
    await database.drop_schema()
    await database.dispose()


async def _create_identity(session: AsyncSession, slug: str) -> tuple[UUID, UUID]:
    organization = Organization(slug=slug, name=slug.title())
    user = User(oidc_subject=f"oidc|{slug}", display_name=slug.title())
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(
            organization_id=organization.id,
            user_id=user.id,
            role=Role.OWNER,
        )
    )
    await session.flush()
    return organization.id, user.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_projects_are_strictly_tenant_scoped(database: Database) -> None:
    async with database.session() as session:
        first_org, first_user = await _create_identity(session, "first")
        second_org, second_user = await _create_identity(session, "second")
        first_project = await ProjectService.create_project(
            session,
            organization_id=first_org,
            user_id=first_user,
            name="Shared name",
        )
        second_project = await ProjectService.create_project(
            session,
            organization_id=second_org,
            user_id=second_user,
            name="Shared name",
        )

    async with database.session() as session:
        first_projects = await ProjectService.list_projects(session, organization_id=first_org)
        second_projects = await ProjectService.list_projects(session, organization_id=second_org)

    assert [project.id for project in first_projects] == [first_project.id]
    assert [project.id for project in second_projects] == [second_project.id]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_project_name_is_a_safe_conflict(database: Database) -> None:
    async with database.session() as session:
        organization_id, user_id = await _create_identity(session, "duplicate")
        await ProjectService.create_project(
            session,
            organization_id=organization_id,
            user_id=user_id,
            name="Corpus work",
        )

    with pytest.raises(ResourceConflictError, match="conflicts with existing data"):
        async with database.session() as session:
            await ProjectService.create_project(
                session,
                organization_id=organization_id,
                user_id=user_id,
                name="Corpus work",
            )

    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Project)) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_corpus_version_persists_original_and_normalized_text(database: Database) -> None:
    prepared = prepare_corpus(
        CorpusImportRequest(
            language="EN_us",
            sentences=("  Hello   world ", "Second sentence."),
        ),
        CorpusImportLimits(max_sentences=10, max_sentence_characters=100),
    )
    async with database.session() as session:
        organization_id, user_id = await _create_identity(session, "corpus")
        project = await ProjectService.create_project(
            session,
            organization_id=organization_id,
            user_id=user_id,
            name="Demo",
        )
        corpus, version = await ProjectService.create_corpus(
            session,
            organization_id=organization_id,
            user_id=user_id,
            project_id=project.id,
            name="English seed",
            prepared=prepared,
        )
        corpus_id = corpus.id
        version_id = version.id

    async with database.session() as session:
        sentences = (
            await session.scalars(
                select(Sentence)
                .where(Sentence.corpus_version_id == version_id)
                .order_by(Sentence.ordinal)
            )
        ).all()

    assert corpus_id is not None
    assert [sentence.original_text for sentence in sentences] == [
        "  Hello   world ",
        "Second sentence.",
    ]
    assert [sentence.normalized_text for sentence in sentences] == [
        "Hello world",
        "Second sentence.",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_tenant_project_is_indistinguishable_from_missing(database: Database) -> None:
    async with database.session() as session:
        first_org, first_user = await _create_identity(session, "owner")
        second_org, second_user = await _create_identity(session, "intruder")
        project = await ProjectService.create_project(
            session,
            organization_id=first_org,
            user_id=first_user,
            name="Private",
        )

    prepared = prepare_corpus(
        CorpusImportRequest(sentences=("private sentence",)),
        CorpusImportLimits(max_sentences=10, max_sentence_characters=100),
    )
    with pytest.raises(ResourceNotFoundError, match="resource was not found"):
        async with database.session() as session:
            await ProjectService.create_corpus(
                session,
                organization_id=second_org,
                user_id=second_user,
                project_id=project.id,
                name="Stolen",
                prepared=prepared,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transaction_rolls_back_on_application_exception(database: Database) -> None:
    async def abort_transaction() -> None:
        async with database.session() as session:
            await _create_identity(session, "rollback")
            raise RuntimeError("abort")

    with pytest.raises(RuntimeError, match="abort"):
        await abort_transaction()

    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Organization)) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_blank_names_are_rejected_before_database_write(database: Database) -> None:
    async with database.session() as session:
        organization_id, user_id = await _create_identity(session, "blank")
        with pytest.raises(ValueError, match="project name"):
            await ProjectService.create_project(
                session,
                organization_id=organization_id,
                user_id=user_id,
                name="   ",
            )
