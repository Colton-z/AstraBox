"""Persistence for artifact_data."""

from ._artifact_repository import ArtifactDocumentRepository


class ArtifactDataRepository(ArtifactDocumentRepository):
    COLLECTION_NAME = "artifact_data"
