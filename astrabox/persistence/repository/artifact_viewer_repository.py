"""Persistence for artifact_viewers."""

from ._artifact_repository import ArtifactDocumentRepository


class ArtifactViewerRepository(ArtifactDocumentRepository):
    COLLECTION_NAME = "artifact_viewers"
