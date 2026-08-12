# Project workspaces and immutable corpus imports

## Delivery status

The project-workspace vertical slice is wired into the API and the accessible `/projects`
workbench. Its service shares the application-owned database with the durable job control
plane, and the lifespan disposes that owner exactly once.

This slice implements only:

- create and list tenant projects;
- create a corpus and immutable version 1 from manual sentences;
- import one bounded UTF-8 TXT, CSV, or JSON file;
- list corpora, versions, and sentences;
- download deterministic TXT, JSON, and spreadsheet-safe CSV exports; and
- schedule an owner/admin-confirmed, retention-safe project deletion lifecycle.

It does **not** implement project/corpus update, individual corpus deletion, new versions of an
existing corpus, bulk archive ingestion, asynchronous import, lineage editing, or a user-facing
deletion undo. Project deletion is logically immediate and physically finalized by maintenance
only after at least 30 days; see the [project deletion runbook](../operations/project-deletion.md).

The browser sentence table deliberately previews at most the first 500 rows; TXT, JSON, and
CSV downloads always contain the complete version. Create/import forms lock while a request
is in flight because these endpoints do not claim idempotent replay semantics.

## Import contracts

All imports are limited to 10 MiB, 10,000 input sentences, and 2,000 normalized characters
per sentence by the default settings. The server applies Unicode NFC normalization,
collapses whitespace, drops blank sentences, and keeps only the first occurrence of a
normalized duplicate. Input order is preserved.

Accepted files are deliberately narrow:

| Format | Required extension | Required media type | Schema |
| --- | --- | --- | --- |
| TXT | `.txt` | `text/plain` | One sentence per line |
| CSV | `.csv` | `text/csv` or `application/csv` | Header row and an explicit existing text-column name |
| JSON | `.json` | `application/json` | Exactly `{"sentences":["…"]}` |

Files must decode as UTF-8 (a UTF-8 BOM is accepted). Mismatched extensions, media types,
duplicate CSV headers, malformed rows, extra JSON keys, non-string JSON values, and archive
formats are rejected. Corpus text is never interpreted as markup or executable content.

## Exports and integrity

Exports always use sentence ordinal order and UTF-8. JSON is canonicalized with sorted keys;
CSV uses a fixed `ordinal,text` header and prefixes spreadsheet formula markers with an
apostrophe. Responses include `Content-Digest` using the RFC 9530 SHA-256 representation,
an exact SHA-256 `ETag`, `X-Content-SHA256`, `Cache-Control: no-store`, and a sanitized
`Content-Disposition` with ASCII and RFC 5987 filenames.

The persisted `content_sha256` identifies the normalized corpus domain payload. The download
headers identify the exact bytes of one export encoding; the two digests intentionally have
different purposes.

## Authorization model

Owner, admin, and editor roles may create. Viewers may list and export. Every service query
resolves the authenticated subject through the organization membership table and scopes the
full project/corpus/version hierarchy by organization. Foreign-tenant identifiers return the
same not-found contract as absent identifiers.

Only owners and admins may request project deletion. The browser presents the explicit danger
control only after reading the server-verified role from `/api/v1/auth/me`; the API remains the
authoritative control. The exact phrase `DELETE <project name>` is required. Pending projects are
immediately hidden and all project-scoped reads and writes fail closed. Nonterminal runs cause a
conflict rather than implicit cancellation.

## Application-factory integration

The application factory constructs one database owner for the default workspace and job
services, while preserving factories for isolated tests:

```python
from corpuskit.api.projects import project_workspace_router

database = Database(resolved.database_url)
job_service = JobControlPlane(database)
workspace_service = ProjectWorkspaceService(database, resolved)
app.state.workspace_service = workspace_service
app.include_router(
    project_workspace_router(
        workspace_service,
        max_upload_bytes=resolved.max_upload_bytes,
    ),
    prefix="/api/v1",
)
```

The lifespan closes `database` exactly once. `workspace_service_factory` lets API tests
inject a fake without opening a database. The API never creates schema at startup; the
Compose migration gate remains responsible for upgrades.

The BFF forwards only validated `Content-Disposition`, `Content-Digest`, `ETag`, and
`X-Content-SHA256` download metadata. Live-stack Playwright remains an explicit release
gate because it requires the API, web BFF, and migrated database to run together.
