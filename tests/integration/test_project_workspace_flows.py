"""Persistence acceptance tests for tenant-scoped immutable project workspaces."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from corpuskit.config import Settings
from corpuskit.domain.errors import InvalidRequestError, ResourceNotFoundError
from corpuskit.domain.workspaces import (
    CorpusExportFormat,
    CorpusFileFormat,
    CorpusUpload,
    ManualCorpusInput,
    ProjectInput,
)
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import Membership, Organization, Role, User
from corpuskit.services.project_workspaces import ProjectWorkspaceService, WorkspaceActor


@pytest_asyncio.fixture
async def database() -> AsyncIterator[Database]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    database = Database("sqlite+aiosqlite:///:memory:", engine=engine)
    await database.create_schema()
    yield database
    await database.drop_schema()
    await database.dispose()


async def _identity(
    session: AsyncSession,
    slug: str,
    *,
    role: Role = Role.OWNER,
) -> WorkspaceActor:
    organization = Organization(slug=slug, name=slug.title())
    user = User(oidc_subject=f"oidc|{slug}", display_name=slug.title())
    session.add_all((organization, user))
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=role))
    await session.flush()
    return WorkspaceActor(subject=user.oidc_subject, organization_id=organization.id)


def _service(database: Database, **overrides: int) -> ProjectWorkspaceService:
    return ProjectWorkspaceService(
        database,
        Settings(environment="test", api_docs_enabled=True, **overrides),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_manual_corpus_round_trip_is_immutable_ordered_and_exportable(
    database: Database,
) -> None:
    async with database.session() as session:
        actor = await _identity(session, "manual")
    service = _service(database)

    project = await service.create_project(
        actor,
        ProjectInput(name="  Speech   lab  ", description="Research workspace"),
    )
    creation = await service.create_manual_corpus(
        actor,
        project.id,
        ManualCorpusInput(
            name="Unicode seed",
            language="EN_us",
            sentences=("  Hello   world ", "", "Hello world", "مرحبا بالعالم"),
        ),
    )

    assert [item.id for item in await service.list_projects(actor)] == [project.id]
    assert [item.id for item in await service.list_corpora(actor, project.id)] == [
        creation.corpus.id
    ]
    assert await service.list_versions(actor, project.id, creation.corpus.id) == (creation.version,)
    sentences = await service.list_sentences(
        actor,
        project.id,
        creation.corpus.id,
        creation.version.id,
        offset=0,
        limit=100,
    )
    assert [item.normalized_text for item in sentences] == ["Hello world", "مرحبا بالعالم"]
    assert [item.original_text for item in sentences] == ["  Hello   world ", "مرحبا بالعالم"]
    assert creation.version.version_number == 1
    assert creation.version.parent_version_id is None

    for export_format in CorpusExportFormat:
        exported = await service.export_version(
            actor,
            project.id,
            creation.corpus.id,
            creation.version.id,
            export_format,
        )
        assert exported.content
        assert len(exported.sha256) == 64
        assert exported.content_disposition.startswith("attachment;")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_format", "content_type", "filename", "content", "text_column", "expected"),
    [
        (CorpusFileFormat.TXT, "text/plain", "seed.txt", b"one\ntwo\n", None, ("one", "two")),
        (
            CorpusFileFormat.CSV,
            "text/csv",
            "seed.csv",
            b"id,text\n1,one\n2,two\n",
            "text",
            ("one", "two"),
        ),
        (
            CorpusFileFormat.JSON,
            "application/json",
            "seed.json",
            b'{"sentences":["one","two"]}',
            None,
            ("one", "two"),
        ),
    ],
)
async def test_each_upload_format_creates_an_initial_version(
    database: Database,
    file_format: CorpusFileFormat,
    content_type: str,
    filename: str,
    content: bytes,
    text_column: str | None,
    expected: tuple[str, ...],
) -> None:
    async with database.session() as session:
        actor = await _identity(session, file_format.value)
    service = _service(database)
    project = await service.create_project(actor, ProjectInput(name="Import demo"))
    creation = await service.import_corpus(
        actor,
        project.id,
        CorpusUpload(
            name=f"{file_format.value} corpus",
            language="en-us",
            filename=filename,
            content_type=content_type,
            file_format=file_format,
            content=content,
            text_column=text_column,
        ),
    )
    sentences = await service.list_sentences(
        actor,
        project.id,
        creation.corpus.id,
        creation.version.id,
        offset=0,
        limit=100,
    )
    assert tuple(item.normalized_text for item in sentences) == expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_tenant_resources_are_indistinguishable_and_viewer_is_read_only(
    database: Database,
) -> None:
    async with database.session() as session:
        owner = await _identity(session, "owner")
        intruder = await _identity(session, "intruder")
        viewer = await _identity(session, "viewer", role=Role.VIEWER)
    service = _service(database)
    project = await service.create_project(owner, ProjectInput(name="Private"))
    creation = await service.create_manual_corpus(
        owner,
        project.id,
        ManualCorpusInput(name="Private corpus", sentences=("secret",)),
    )

    operations = (
        service.list_corpora(intruder, project.id),
        service.list_versions(intruder, project.id, creation.corpus.id),
        service.list_sentences(
            intruder,
            project.id,
            creation.corpus.id,
            creation.version.id,
            offset=0,
            limit=100,
        ),
        service.export_version(
            intruder,
            project.id,
            creation.corpus.id,
            creation.version.id,
            CorpusExportFormat.TXT,
        ),
    )
    for operation in operations:
        with pytest.raises(ResourceNotFoundError, match="resource was not found"):
            await operation

    assert await service.list_projects(viewer) == ()
    with pytest.raises(ResourceNotFoundError):
        await service.create_project(viewer, ProjectInput(name="Forbidden"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_limits_are_enforced_before_persistence(database: Database) -> None:
    async with database.session() as session:
        actor = await _identity(session, "limits")
    service = _service(
        database,
        max_upload_bytes=7,
        max_sentences_per_import=2,
        max_sentence_characters=4,
    )
    project = await service.create_project(actor, ProjectInput(name="Limits"))

    with pytest.raises(InvalidRequestError):
        await service.create_project(actor, ProjectInput(name="\x00"))
    with pytest.raises(InvalidRequestError):
        await service.create_project(
            actor,
            ProjectInput(name="Bad description", description="\x00"),
        )
    with pytest.raises(InvalidRequestError):
        await service.import_corpus(
            actor,
            project.id,
            CorpusUpload(
                name="Large",
                filename="large.txt",
                content_type="text/plain",
                file_format=CorpusFileFormat.TXT,
                content=b"123456789",
            ),
        )
    with pytest.raises(InvalidRequestError):
        await service.create_manual_corpus(
            actor,
            project.id,
            ManualCorpusInput(name="Long", sentences=("12345",)),
        )
    with pytest.raises(InvalidRequestError):
        await service.create_manual_corpus(
            actor,
            project.id,
            ManualCorpusInput(name="Too many bytes", sentences=("1234", "5678")),
        )
    assert await service.list_corpora(actor, project.id) == ()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_identity_is_not_treated_as_an_empty_tenant(database: Database) -> None:
    service = _service(database)
    actor = WorkspaceActor(
        subject="missing",
        organization_id=UUID("00000000-0000-4000-8000-000000000099"),
    )
    with pytest.raises(ResourceNotFoundError, match="resource was not found"):
        await service.list_projects(actor)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.EDITOR])
async def test_each_writer_membership_can_create(database: Database, role: Role) -> None:
    async with database.session() as session:
        actor = await _identity(session, f"writer-{role.value}", role=role)
    service = _service(database)
    project = await service.create_project(actor, ProjectInput(name=f"{role.value} project"))
    creation = await service.create_manual_corpus(
        actor,
        project.id,
        ManualCorpusInput(name=f"{role.value} corpus", sentences=("Hello",)),
    )
    assert creation.version.version_number == 1
