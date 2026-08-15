"""Project lifecycle serialization stays deterministic and least-privileged."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.domain.corpora import PreparedCorpus, PreparedSentence
from corpuskit.domain.errors import ResourceConflictError
from corpuskit.persistence.models import Corpus, CorpusVersion, Project
from corpuskit.services.project_lifecycle import lock_project_lifecycle
from corpuskit.services.projects import ProjectService

ORGANIZATION_ID = UUID("00000000-0000-4000-8000-000000000001")
USER_ID = UUID("00000000-0000-4000-8000-000000000002")
PROJECT_ID = UUID("00000000-0000-4000-8000-000000000123")
CORPUS_ID = UUID("00000000-0000-4000-8000-000000000124")
VERSION_ID = UUID("00000000-0000-4000-8000-000000000125")
OPERATION = "corpus.version.create"


def _prepared_corpus() -> PreparedCorpus:
    return PreparedCorpus(
        language="en-us",
        sentences=(
            PreparedSentence(
                ordinal=0,
                original_text="New sentence",
                normalized_text="New sentence",
            ),
        ),
        content_sha256="0" * 64,
    )


@pytest.mark.asyncio
async def test_postgres_project_lock_uses_transaction_advisory_lock() -> None:
    session = Mock(spec=AsyncSession)
    session.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    session.scalar = AsyncMock(return_value=None)

    await lock_project_lifecycle(session, PROJECT_ID)
    await lock_project_lifecycle(session, PROJECT_ID)

    first = session.scalar.await_args_list[0].args[0]
    second = session.scalar.await_args_list[1].args[0]
    compiled = first.compile(dialect=postgresql.dialect())
    assert "pg_advisory_xact_lock" in str(compiled)
    assert tuple(compiled.params.values()) == tuple(
        second.compile(dialect=postgresql.dialect()).params.values()
    )


@pytest.mark.asyncio
async def test_sqlite_project_lock_is_an_explicit_noop() -> None:
    session = Mock(spec=AsyncSession)
    session.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    session.scalar = AsyncMock(return_value=None)

    await lock_project_lifecycle(
        session,
        PROJECT_ID,
    )

    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_row_lock_targets_the_selected_postgres_table() -> None:
    session = Mock(spec=AsyncSession)
    project = Mock(spec=Project)
    session.scalar = AsyncMock(return_value=project)

    resolved = await ProjectService._require_project(
        session,
        organization_id=ORGANIZATION_ID,
        project_id=PROJECT_ID,
        operation=OPERATION,
        for_update=True,
    )

    statement = session.scalar.await_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert resolved is project
    assert "FROM projects" in compiled
    assert "FOR UPDATE OF projects" in compiled
    assert "FOR UPDATE OF corpora" not in compiled


@pytest.mark.asyncio
async def test_create_version_rejects_missing_parent_without_locking_corpus() -> None:
    session = Mock(spec=AsyncSession)
    corpus = Mock(spec=Corpus)
    corpus.id = CORPUS_ID
    session.scalar = AsyncMock(side_effect=(Mock(spec=Project), corpus, None))
    session.flush = AsyncMock()

    with pytest.raises(ResourceConflictError) as error:
        await ProjectService.create_version(
            session,
            organization_id=ORGANIZATION_ID,
            user_id=USER_ID,
            project_id=PROJECT_ID,
            corpus_id=CORPUS_ID,
            prepared=_prepared_corpus(),
            operation=OPERATION,
        )

    corpus_lookup = session.scalar.await_args_list[1].args[0]
    compiled = str(corpus_lookup.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" not in compiled
    assert error.value.operation == OPERATION
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_version_maps_flush_integrity_error_to_conflict() -> None:
    session = Mock(spec=AsyncSession)
    corpus = Mock(spec=Corpus)
    corpus.id = CORPUS_ID
    latest = Mock(spec=CorpusVersion)
    latest.id = VERSION_ID
    latest.version_number = 1
    failure = IntegrityError("INSERT", {}, RuntimeError("duplicate version"))
    session.scalar = AsyncMock(side_effect=(Mock(spec=Project), corpus, latest))
    session.flush = AsyncMock(side_effect=failure)

    with pytest.raises(ResourceConflictError) as error:
        await ProjectService.create_version(
            session,
            organization_id=ORGANIZATION_ID,
            user_id=USER_ID,
            project_id=PROJECT_ID,
            corpus_id=CORPUS_ID,
            prepared=_prepared_corpus(),
            operation=OPERATION,
        )

    assert error.value.operation == OPERATION
    assert error.value.__cause__ is failure
    session.add.assert_called_once()
