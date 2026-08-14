"""Tenant-scoped project and corpus persistence services."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.domain.corpora import PreparedCorpus
from corpuskit.domain.errors import ResourceConflictError, ResourceNotFoundError
from corpuskit.domain.workspaces import ProjectLifecycle
from corpuskit.persistence.models import Corpus, CorpusVersion, Project, Sentence


class ProjectService:
    """Perform project operations with organization scoping in every query."""

    @staticmethod
    async def create_project(
        session: AsyncSession,
        *,
        organization_id: UUID,
        user_id: UUID,
        name: str,
        description: str = "",
    ) -> Project:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("project name must not be blank")
        project = Project(
            organization_id=organization_id,
            created_by=user_id,
            name=normalized_name,
            description=description.strip(),
        )
        session.add(project)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise ResourceConflictError("create_project") from exc
        return project

    @staticmethod
    async def list_projects(session: AsyncSession, *, organization_id: UUID) -> tuple[Project, ...]:
        statement = (
            select(Project)
            .where(
                Project.organization_id == organization_id,
                Project.lifecycle_state == ProjectLifecycle.ACTIVE,
            )
            .order_by(Project.created_at, Project.id)
        )
        return tuple((await session.scalars(statement)).all())

    @staticmethod
    async def list_corpora(
        session: AsyncSession,
        *,
        organization_id: UUID,
        project_id: UUID,
    ) -> tuple[Corpus, ...]:
        """List corpora only after proving the parent project belongs to the tenant."""

        await ProjectService._require_project(
            session,
            organization_id=organization_id,
            project_id=project_id,
            operation="list_corpora",
        )
        statement = (
            select(Corpus)
            .where(
                Corpus.organization_id == organization_id,
                Corpus.project_id == project_id,
            )
            .order_by(Corpus.created_at, Corpus.id)
        )
        return tuple((await session.scalars(statement)).all())

    @staticmethod
    async def list_versions(
        session: AsyncSession,
        *,
        organization_id: UUID,
        project_id: UUID,
        corpus_id: UUID,
    ) -> tuple[CorpusVersion, ...]:
        """List immutable versions through a fully tenant-scoped corpus lookup."""

        await ProjectService._require_corpus(
            session,
            organization_id=organization_id,
            project_id=project_id,
            corpus_id=corpus_id,
            operation="list_corpus_versions",
        )
        statement = (
            select(CorpusVersion)
            .where(
                CorpusVersion.organization_id == organization_id,
                CorpusVersion.corpus_id == corpus_id,
            )
            .order_by(CorpusVersion.version_number, CorpusVersion.id)
        )
        return tuple((await session.scalars(statement)).all())

    @staticmethod
    async def list_sentences(
        session: AsyncSession,
        *,
        organization_id: UUID,
        project_id: UUID,
        corpus_id: UUID,
        version_id: UUID,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[Sentence, ...]:
        """Page sentences in deterministic ordinal order after checking the whole hierarchy."""

        await ProjectService.get_version(
            session,
            organization_id=organization_id,
            project_id=project_id,
            corpus_id=corpus_id,
            version_id=version_id,
            operation="list_sentences",
        )
        statement = (
            select(Sentence)
            .where(
                Sentence.organization_id == organization_id,
                Sentence.corpus_version_id == version_id,
            )
            .order_by(Sentence.ordinal, Sentence.id)
            .offset(offset)
            .limit(limit)
        )
        return tuple((await session.scalars(statement)).all())

    @staticmethod
    async def get_version(
        session: AsyncSession,
        *,
        organization_id: UUID,
        project_id: UUID,
        corpus_id: UUID,
        version_id: UUID,
        operation: str,
    ) -> CorpusVersion:
        """Resolve a version through tenant, project, and corpus ownership in one query."""

        statement = (
            select(CorpusVersion)
            .join(Corpus, Corpus.id == CorpusVersion.corpus_id)
            .join(Project, Project.id == Corpus.project_id)
            .where(
                CorpusVersion.id == version_id,
                CorpusVersion.organization_id == organization_id,
                CorpusVersion.corpus_id == corpus_id,
                Corpus.organization_id == organization_id,
                Corpus.project_id == project_id,
                Project.organization_id == organization_id,
                Project.lifecycle_state == ProjectLifecycle.ACTIVE,
            )
        )
        version = await session.scalar(statement)
        if version is None:
            raise ResourceNotFoundError(operation)
        return version

    @staticmethod
    async def get_corpus(
        session: AsyncSession,
        *,
        organization_id: UUID,
        project_id: UUID,
        corpus_id: UUID,
        operation: str,
    ) -> Corpus:
        """Resolve a corpus without disclosing whether another tenant owns its identifier."""

        return await ProjectService._require_corpus(
            session,
            organization_id=organization_id,
            project_id=project_id,
            corpus_id=corpus_id,
            operation=operation,
        )

    @staticmethod
    async def create_corpus(
        session: AsyncSession,
        *,
        organization_id: UUID,
        user_id: UUID,
        project_id: UUID,
        name: str,
        prepared: PreparedCorpus,
    ) -> tuple[Corpus, CorpusVersion]:
        await ProjectService._require_project(
            session,
            organization_id=organization_id,
            project_id=project_id,
            operation="create_corpus",
            for_update=True,
        )

        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("corpus name must not be blank")
        corpus = Corpus(
            organization_id=organization_id,
            project_id=project_id,
            created_by=user_id,
            name=normalized_name,
        )
        version = CorpusVersion(
            organization_id=organization_id,
            created_by=user_id,
            version_number=1,
            language=prepared.language,
            sentence_count=len(prepared.sentences),
            content_sha256=prepared.content_sha256,
        )
        version.sentences = [
            Sentence(
                organization_id=organization_id,
                ordinal=sentence.ordinal,
                original_text=sentence.original_text,
                normalized_text=sentence.normalized_text,
            )
            for sentence in prepared.sentences
        ]
        corpus.versions.append(version)
        session.add(corpus)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise ResourceConflictError("create_corpus") from exc
        return corpus, version

    @staticmethod
    async def create_version(
        session: AsyncSession,
        *,
        organization_id: UUID,
        user_id: UUID,
        project_id: UUID,
        corpus_id: UUID,
        prepared: PreparedCorpus,
        operation: str,
    ) -> CorpusVersion:
        """Append one immutable version while serializing writers per corpus."""

        await ProjectService._require_project(
            session,
            organization_id=organization_id,
            project_id=project_id,
            operation=operation,
            for_update=True,
        )
        corpus = await ProjectService._require_corpus(
            session,
            organization_id=organization_id,
            project_id=project_id,
            corpus_id=corpus_id,
            operation=operation,
            for_update=True,
        )
        latest = await session.scalar(
            select(CorpusVersion)
            .where(
                CorpusVersion.organization_id == organization_id,
                CorpusVersion.corpus_id == corpus.id,
            )
            .order_by(CorpusVersion.version_number.desc(), CorpusVersion.id.desc())
            .limit(1)
        )
        if latest is None:
            raise ResourceConflictError(operation)

        version = CorpusVersion(
            organization_id=organization_id,
            corpus_id=corpus.id,
            parent_version_id=latest.id,
            created_by=user_id,
            version_number=latest.version_number + 1,
            language=prepared.language,
            sentence_count=len(prepared.sentences),
            content_sha256=prepared.content_sha256,
        )
        version.sentences = [
            Sentence(
                organization_id=organization_id,
                ordinal=sentence.ordinal,
                original_text=sentence.original_text,
                normalized_text=sentence.normalized_text,
            )
            for sentence in prepared.sentences
        ]
        session.add(version)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise ResourceConflictError(operation) from exc
        return version

    @staticmethod
    async def _require_project(
        session: AsyncSession,
        *,
        organization_id: UUID,
        project_id: UUID,
        operation: str,
        for_update: bool = False,
    ) -> Project:
        statement = select(Project).where(
            Project.id == project_id,
            Project.organization_id == organization_id,
            Project.lifecycle_state == ProjectLifecycle.ACTIVE,
        )
        if for_update:
            statement = statement.with_for_update(of=Project)
        project = await session.scalar(statement)
        if project is None:
            raise ResourceNotFoundError(operation)
        return project

    @staticmethod
    async def _require_corpus(
        session: AsyncSession,
        *,
        organization_id: UUID,
        project_id: UUID,
        corpus_id: UUID,
        operation: str,
        for_update: bool = False,
    ) -> Corpus:
        statement = (
            select(Corpus)
            .join(Project, Project.id == Corpus.project_id)
            .where(
                Corpus.id == corpus_id,
                Corpus.organization_id == organization_id,
                Corpus.project_id == project_id,
                Project.organization_id == organization_id,
                Project.lifecycle_state == ProjectLifecycle.ACTIVE,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        corpus = await session.scalar(statement)
        if corpus is None:
            raise ResourceNotFoundError(operation)
        return corpus
