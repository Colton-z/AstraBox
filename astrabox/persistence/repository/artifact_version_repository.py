"""Persistence for artifact_versions."""

from ._artifact_repository import ArtifactDocumentRepository


class ArtifactVersionRepository(ArtifactDocumentRepository):
    COLLECTION_NAME = "artifact_versions"
