"""Strongly named identifiers shared across domain and persistence layers."""

from __future__ import annotations

from typing import NewType
from uuid import UUID

OrganizationId = NewType("OrganizationId", UUID)
UserId = NewType("UserId", UUID)
ProjectId = NewType("ProjectId", UUID)
CorpusId = NewType("CorpusId", UUID)
CorpusVersionId = NewType("CorpusVersionId", UUID)
RunId = NewType("RunId", UUID)
ArtifactId = NewType("ArtifactId", UUID)
