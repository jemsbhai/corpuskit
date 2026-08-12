"""Tenant-scoped project workspaces, bounded imports, and deterministic exports."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePath
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.config import Settings
from corpuskit.domain.corpora import (
    CorpusImportLimits,
    CorpusImportRequest,
    PreparedCorpus,
    prepare_corpus,
)
from corpuskit.domain.errors import InvalidRequestError, ResourceNotFoundError
from corpuskit.domain.platform import AuditAction, AuditResourceType
from corpuskit.domain.workspaces import (
    CorpusExportFormat,
    CorpusFileFormat,
    CorpusUpload,
    ManualCorpusInput,
    ProjectDeletionInput,
    ProjectInput,
)
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import Membership, Role, Sentence, User
from corpuskit.persistence.tenant_context import TenantContext
from corpuskit.services.platform import AuditIdentity, AuditWriter, QuotaManager
from corpuskit.services.project_deletion import (
    ProjectDeletionService,
    ProjectDeletionSnapshot,
)
from corpuskit.services.projects import ProjectService

_WRITER_ROLES = frozenset({Role.OWNER, Role.ADMIN, Role.EDITOR})
_ADMIN_ROLES = frozenset({Role.OWNER, Role.ADMIN})
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_FORMAT_MEDIA_TYPES: dict[CorpusFileFormat, frozenset[str]] = {
    CorpusFileFormat.TXT: frozenset({"text/plain"}),
    CorpusFileFormat.CSV: frozenset({"text/csv", "application/csv"}),
    CorpusFileFormat.JSON: frozenset({"application/json"}),
}
_EXPORT_MEDIA_TYPES: dict[CorpusExportFormat, str] = {
    CorpusExportFormat.TXT: "text/plain; charset=utf-8",
    CorpusExportFormat.JSON: "application/json; charset=utf-8",
    CorpusExportFormat.CSV: "text/csv; charset=utf-8",
}


@dataclass(frozen=True, slots=True)
class WorkspaceActor:
    """Verified identity fields needed at the persistence boundary."""

    subject: str
    organization_id: UUID
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    id: UUID
    name: str
    description: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CorpusSnapshot:
    id: UUID
    project_id: UUID
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VersionSnapshot:
    id: UUID
    corpus_id: UUID
    parent_version_id: UUID | None
    version_number: int
    language: str
    sentence_count: int
    content_sha256: str
    corpusgen_version: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SentenceSnapshot:
    ordinal: int
    original_text: str
    normalized_text: str


@dataclass(frozen=True, slots=True)
class CorpusCreation:
    corpus: CorpusSnapshot
    version: VersionSnapshot


@dataclass(frozen=True, slots=True)
class ExportedCorpus:
    """An immutable byte representation and its response metadata."""

    content: bytes
    media_type: str
    filename: str
    content_disposition: str
    sha256: str
    content_digest: str


class ProjectWorkspaceService:
    """Coordinate authorization, normalization, persistence, and export encoding."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self._limits = CorpusImportLimits(
            max_sentences=settings.max_sentences_per_import,
            max_sentence_characters=settings.max_sentence_characters,
        )
        self._max_upload_bytes = settings.max_upload_bytes
        self._deletion_retention = timedelta(days=settings.artifact_retention_days)

    async def close(self) -> None:
        await self.database.dispose()

    async def create_project(
        self,
        actor: WorkspaceActor,
        request: ProjectInput,
    ) -> ProjectSnapshot:
        name = _label(request.name, "project.create")
        description = _description(request.description, "project.create")
        async with self.database.session(_context(actor)) as session:
            user_id, role = await self._actor(session, actor)
            _require_writer(role, "project.create")
            project = await ProjectService.create_project(
                session,
                organization_id=actor.organization_id,
                user_id=user_id,
                name=name,
                description=description,
            )
            await AuditWriter.append(
                session,
                organization_id=actor.organization_id,
                actor=AuditIdentity.user(user_id),
                action=AuditAction.PROJECT_CREATED,
                resource_type=AuditResourceType.PROJECT,
                resource_id=project.id,
                request_id=actor.request_id,
            )
            return _project_snapshot(project)

    async def list_projects(self, actor: WorkspaceActor) -> tuple[ProjectSnapshot, ...]:
        async with self.database.session(_context(actor)) as session:
            await self._actor(session, actor)
            projects = await ProjectService.list_projects(
                session,
                organization_id=actor.organization_id,
            )
            return tuple(_project_snapshot(project) for project in projects)

    async def request_project_deletion(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        request: ProjectDeletionInput,
    ) -> ProjectDeletionSnapshot:
        async with self.database.session(_context(actor)) as session:
            user_id, role = await self._actor(session, actor)
            _require_admin(role, "project.delete")
            return await ProjectDeletionService.request(
                session,
                organization_id=actor.organization_id,
                user_id=user_id,
                project_id=project_id,
                confirmation=request.confirmation,
                request_id=actor.request_id,
                retention=self._deletion_retention,
            )

    async def create_manual_corpus(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        request: ManualCorpusInput,
    ) -> CorpusCreation:
        if _utf8_size(request.sentences) > self._max_upload_bytes:
            raise InvalidRequestError("corpus.create")
        prepared = self._prepare(request.language, request.sentences, "corpus.create")
        return await self._persist_corpus(actor, project_id, request.name, prepared)

    async def import_corpus(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        upload: CorpusUpload,
    ) -> CorpusCreation:
        operation = "corpus.import"
        if not upload.content or len(upload.content) > self._max_upload_bytes:
            raise InvalidRequestError(operation)
        sentences = parse_corpus_upload(upload, operation=operation)
        prepared = self._prepare(upload.language, sentences, operation)
        return await self._persist_corpus(actor, project_id, upload.name, prepared)

    async def list_corpora(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
    ) -> tuple[CorpusSnapshot, ...]:
        async with self.database.session(_context(actor)) as session:
            await self._actor(session, actor)
            corpora = await ProjectService.list_corpora(
                session,
                organization_id=actor.organization_id,
                project_id=project_id,
            )
            return tuple(_corpus_snapshot(corpus) for corpus in corpora)

    async def list_versions(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        corpus_id: UUID,
    ) -> tuple[VersionSnapshot, ...]:
        async with self.database.session(_context(actor)) as session:
            await self._actor(session, actor)
            versions = await ProjectService.list_versions(
                session,
                organization_id=actor.organization_id,
                project_id=project_id,
                corpus_id=corpus_id,
            )
            return tuple(_version_snapshot(version) for version in versions)

    async def list_sentences(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        corpus_id: UUID,
        version_id: UUID,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[SentenceSnapshot, ...]:
        async with self.database.session(_context(actor)) as session:
            await self._actor(session, actor)
            sentences = await ProjectService.list_sentences(
                session,
                organization_id=actor.organization_id,
                project_id=project_id,
                corpus_id=corpus_id,
                version_id=version_id,
                offset=offset,
                limit=limit,
            )
            return tuple(_sentence_snapshot(sentence) for sentence in sentences)

    async def export_version(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        corpus_id: UUID,
        version_id: UUID,
        export_format: CorpusExportFormat,
    ) -> ExportedCorpus:
        operation = "corpus.export"
        async with self.database.session(_context(actor)) as session:
            await self._actor(session, actor)
            corpus = await ProjectService.get_corpus(
                session,
                organization_id=actor.organization_id,
                project_id=project_id,
                corpus_id=corpus_id,
                operation=operation,
            )
            version = await ProjectService.get_version(
                session,
                organization_id=actor.organization_id,
                project_id=project_id,
                corpus_id=corpus_id,
                version_id=version_id,
                operation=operation,
            )
            sentences = await ProjectService.list_sentences(
                session,
                organization_id=actor.organization_id,
                project_id=project_id,
                corpus_id=corpus_id,
                version_id=version_id,
                offset=0,
                limit=version.sentence_count,
            )
            return build_export(
                corpus_id=corpus.id,
                corpus_name=corpus.name,
                version=_version_snapshot(version),
                sentences=tuple(_sentence_snapshot(sentence) for sentence in sentences),
                export_format=export_format,
            )

    async def _persist_corpus(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        name: str,
        prepared: PreparedCorpus,
    ) -> CorpusCreation:
        safe_name = _label(name, "corpus.create")
        async with self.database.session(_context(actor)) as session:
            user_id, role = await self._actor(session, actor)
            _require_writer(role, "corpus.create")
            await QuotaManager.consume_corpus_sentences(
                session,
                organization_id=actor.organization_id,
                sentence_count=len(prepared.sentences),
            )
            corpus, version = await ProjectService.create_corpus(
                session,
                organization_id=actor.organization_id,
                user_id=user_id,
                project_id=project_id,
                name=safe_name,
                prepared=prepared,
            )
            await AuditWriter.append(
                session,
                organization_id=actor.organization_id,
                actor=AuditIdentity.user(user_id),
                action=AuditAction.CORPUS_CREATED,
                resource_type=AuditResourceType.CORPUS,
                resource_id=corpus.id,
                request_id=actor.request_id,
                metadata={
                    "content_sha256": version.content_sha256,
                    "language": version.language,
                    "sentence_count": version.sentence_count,
                },
            )
            return CorpusCreation(
                corpus=_corpus_snapshot(corpus),
                version=_version_snapshot(version),
            )

    def _prepare(
        self,
        language: str,
        sentences: tuple[str, ...],
        operation: str,
    ) -> PreparedCorpus:
        try:
            request = CorpusImportRequest(language=language, sentences=sentences)
            return prepare_corpus(request, self._limits)
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError(operation) from exc

    @staticmethod
    async def _actor(
        session: AsyncSession,
        actor: WorkspaceActor,
    ) -> tuple[UUID, Role]:
        row = (
            await session.execute(
                select(User.id, Membership.role)
                .join(Membership, Membership.user_id == User.id)
                .where(
                    User.oidc_subject == actor.subject,
                    Membership.organization_id == actor.organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise ResourceNotFoundError("workspace.identity")
        return row._tuple()


def parse_corpus_upload(
    upload: CorpusUpload, *, operation: str = "corpus.import"
) -> tuple[str, ...]:
    """Validate extension, media type, UTF-8, and a format-specific sentence schema."""

    expected_extension = f".{upload.file_format.value}"
    filename = PurePath(upload.filename.replace("\\", "/")).name
    if (
        not filename
        or _CONTROL_CHARACTERS.search(filename)
        or not filename.lower().endswith(expected_extension)
    ):
        raise InvalidRequestError(operation)
    media_type = upload.content_type.partition(";")[0].strip().lower()
    if media_type not in _FORMAT_MEDIA_TYPES[upload.file_format]:
        raise InvalidRequestError(operation)
    try:
        decoded = upload.content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidRequestError(operation) from exc

    if upload.file_format is CorpusFileFormat.TXT:
        if upload.text_column is not None:
            raise InvalidRequestError(operation)
        return tuple(decoded.splitlines())
    if upload.file_format is CorpusFileFormat.CSV:
        if upload.text_column is None or _CONTROL_CHARACTERS.search(upload.text_column):
            raise InvalidRequestError(operation)
        return _parse_csv(decoded, upload.text_column, operation)
    if upload.text_column is not None:
        raise InvalidRequestError(operation)
    return _parse_json(decoded, operation)


def _parse_csv(decoded: str, text_column: str, operation: str) -> tuple[str, ...]:
    try:
        reader = csv.DictReader(io.StringIO(decoded, newline=""), strict=True)
        headers = reader.fieldnames
        if (
            headers is None
            or not headers
            or len(headers) != len(set(headers))
            or any(not header or _CONTROL_CHARACTERS.search(header) for header in headers)
            or text_column not in headers
        ):
            raise InvalidRequestError(operation)
        values: list[str] = []
        for row in reader:
            if None in row or row.get(text_column) is None:
                raise InvalidRequestError(operation)
            values.append(row[text_column])
    except (csv.Error, UnicodeError) as exc:
        raise InvalidRequestError(operation) from exc
    return tuple(values)


def _parse_json(decoded: str, operation: str) -> tuple[str, ...]:
    try:
        value = json.loads(decoded)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise InvalidRequestError(operation) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"sentences"}
        or not isinstance(value["sentences"], list)
        or not all(isinstance(sentence, str) for sentence in value["sentences"])
    ):
        raise InvalidRequestError(operation)
    return tuple(value["sentences"])


def build_export(
    *,
    corpus_id: UUID,
    corpus_name: str,
    version: VersionSnapshot,
    sentences: tuple[SentenceSnapshot, ...],
    export_format: CorpusExportFormat,
) -> ExportedCorpus:
    """Encode a version in ordinal order and attach verifiable integrity metadata."""

    ordered = tuple(sorted(sentences, key=lambda sentence: sentence.ordinal))
    if export_format is CorpusExportFormat.TXT:
        content = ("\n".join(sentence.normalized_text for sentence in ordered) + "\n").encode(
            "utf-8"
        )
    elif export_format is CorpusExportFormat.JSON:
        value = {
            "corpus": {"id": str(corpus_id), "name": corpus_name},
            "schema_version": "corpuskit.corpus-export.v1",
            "sentences": [
                {"ordinal": sentence.ordinal, "text": sentence.normalized_text}
                for sentence in ordered
            ],
            "version": {
                "content_sha256": version.content_sha256,
                "id": str(version.id),
                "language": version.language,
                "number": version.version_number,
            },
        }
        content = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
    else:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("ordinal", "text"))
        for sentence in ordered:
            writer.writerow((sentence.ordinal, _neutralize_csv_formula(sentence.normalized_text)))
        content = output.getvalue().encode("utf-8")

    sha256 = hashlib.sha256(content).hexdigest()
    digest_value = base64.b64encode(bytes.fromhex(sha256)).decode("ascii")
    filename, disposition = _download_name(
        corpus_name,
        version.version_number,
        export_format.value,
    )
    return ExportedCorpus(
        content=content,
        media_type=_EXPORT_MEDIA_TYPES[export_format],
        filename=filename,
        content_disposition=disposition,
        sha256=sha256,
        content_digest=f"sha-256=:{digest_value}:",
    )


def _neutralize_csv_formula(value: str) -> str:
    if value.startswith(_CSV_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def _download_name(name: str, version_number: int, extension: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFKC", name)
    safe_unicode = "-".join(normalized.split())
    safe_unicode = "".join(
        character
        for character in safe_unicode
        if character not in {'"', "'", "/", "\\", ";"}
        and unicodedata.category(character) not in {"Cc", "Cf"}
    ).strip(".-")
    if not safe_unicode:
        safe_unicode = "corpus"
    safe_unicode = safe_unicode[:80]
    unicode_filename = f"{safe_unicode}-v{version_number}.{extension}"
    ascii_stem = unicodedata.normalize("NFKD", safe_unicode).encode("ascii", "ignore").decode()
    ascii_stem = _FILENAME_UNSAFE.sub("-", ascii_stem).strip(".-") or "corpus"
    filename = f"{ascii_stem[:60]}-v{version_number}.{extension}"
    encoded = quote(unicode_filename, safe="")
    return filename, f"attachment; filename=\"{filename}\"; filename*=UTF-8''{encoded}"


def _project_snapshot(project: object) -> ProjectSnapshot:
    from corpuskit.persistence.models import Project

    if not isinstance(project, Project):
        raise TypeError("project model is required")
    return ProjectSnapshot(
        id=project.id,
        name=project.name,
        description=project.description,
        created_at=project.created_at,
    )


def _corpus_snapshot(corpus: object) -> CorpusSnapshot:
    from corpuskit.persistence.models import Corpus

    if not isinstance(corpus, Corpus):
        raise TypeError("corpus model is required")
    return CorpusSnapshot(
        id=corpus.id,
        project_id=corpus.project_id,
        name=corpus.name,
        created_at=corpus.created_at,
    )


def _version_snapshot(version: object) -> VersionSnapshot:
    from corpuskit.persistence.models import CorpusVersion

    if not isinstance(version, CorpusVersion):
        raise TypeError("corpus version model is required")
    return VersionSnapshot(
        id=version.id,
        corpus_id=version.corpus_id,
        parent_version_id=version.parent_version_id,
        version_number=version.version_number,
        language=version.language,
        sentence_count=version.sentence_count,
        content_sha256=version.content_sha256,
        corpusgen_version=version.corpusgen_version,
        created_at=version.created_at,
    )


def _sentence_snapshot(sentence: Sentence) -> SentenceSnapshot:
    return SentenceSnapshot(
        ordinal=sentence.ordinal,
        original_text=sentence.original_text,
        normalized_text=sentence.normalized_text,
    )


def _label(value: str, operation: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    if not normalized or len(normalized) > 160 or _CONTROL_CHARACTERS.search(normalized):
        raise InvalidRequestError(operation)
    return normalized


def _description(value: str, operation: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    if len(normalized) > 4_000 or "\x00" in normalized:
        raise InvalidRequestError(operation)
    return normalized


def _utf8_size(values: tuple[str, ...]) -> int:
    return sum(len(value.encode("utf-8")) for value in values)


def _require_writer(role: Role, operation: str) -> None:
    if role not in _WRITER_ROLES:
        raise ResourceNotFoundError(operation)


def _require_admin(role: Role, operation: str) -> None:
    if role not in _ADMIN_ROLES:
        raise ResourceNotFoundError(operation)


def _context(actor: WorkspaceActor) -> TenantContext:
    return TenantContext.user(actor.organization_id, actor.subject)


__all__ = [
    "CorpusCreation",
    "CorpusSnapshot",
    "ExportedCorpus",
    "ProjectDeletionSnapshot",
    "ProjectSnapshot",
    "ProjectWorkspaceService",
    "SentenceSnapshot",
    "VersionSnapshot",
    "WorkspaceActor",
    "build_export",
    "parse_corpus_upload",
]
