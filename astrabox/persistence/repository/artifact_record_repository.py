"""Persistence for artifact_records."""

from ._artifact_repository import ArtifactDocumentRepository


class ArtifactRecordRepository(ArtifactDocumentRepository):
    COLLECTION_NAME = "artifact_records"
