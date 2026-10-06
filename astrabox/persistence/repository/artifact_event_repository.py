"""Persistence for artifact_events."""

from ._artifact_repository import ArtifactDocumentRepository


class ArtifactEventRepository(ArtifactDocumentRepository):
    COLLECTION_NAME = "artifact_events"
