"""``astrabox.yaml``: the declarative document ``apply`` and ``diff`` read.

The document's own keys are a closed set, checked here. The fields *inside*
each resource are not: those are whatever the deployment's schema endpoint
declares (``astrabox schema agent``), and the deployment validates them on
write. Restating that field list here would create a second copy that drifts
from the one the console and the write path share.

Resources are identified by ``name`` because ids are server-generated: a
document that named ids could not be written before its first apply.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrabox.cli.output import EXIT_USAGE, CliError

#: The document contract this CLI reads. Bumped when the top-level shape
#: changes, independently of the product version.
DOCUMENT_VERSION = 1

#: Top-level keys the document may carry. An unknown key is refused rather
#: than ignored: a typo in a resource list name would otherwise apply a
#: document that silently declares nothing.
_TOP_LEVEL_KEYS = frozenset({"version", "environments", "agents"})

#: Resource kinds in apply order. Environments come first because an Agent's
#: ``environment_name`` is validated against an existing environment.
KIND_ENVIRONMENT = "environment"
KIND_AGENT = "agent"


@dataclass(frozen=True)
class ResourceSpec:
    """One declared resource: its kind, its identity, and its fields.

    ``fields`` is the document body as written, including ``name``. It is sent
    to the deployment unchanged — the CLI never adds a default of its own,
    because a field the document did not set is one the deployment's own
    default should decide.
    """

    kind: str
    name: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class Document:
    """A parsed, structurally valid ``astrabox.yaml``."""

    version: int
    environments: tuple[ResourceSpec, ...]
    agents: tuple[ResourceSpec, ...]
    path: Path | None = None

    def resources(self) -> tuple[ResourceSpec, ...]:
        """Every declared resource in apply order."""
        return self.environments + self.agents


def load_document(path: str | Path) -> Document:
    """Read and structurally validate one ``astrabox.yaml``.

    Raises :class:`~astrabox.cli.output.CliError` with the usage exit code for
    an unreadable file, invalid YAML, an unknown document version, an unknown
    top-level key, a resource without a name, or a name declared twice within
    one kind. Every one of these is checked before any request is sent: a
    document that applies half of itself and then fails is worse than one that
    applies none.
    """
    import yaml

    document_path = Path(path)
    try:
        raw_text = document_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot read {document_path}: {exc}", exit_code=EXIT_USAGE) from exc

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise CliError(f"{document_path} is not valid YAML: {exc}", exit_code=EXIT_USAGE) from exc

    return parse_document(raw, path=document_path)


def parse_document(raw: Any, *, path: Path | None = None) -> Document:
    """Validate an already-decoded document body.

    Separated from :func:`load_document` so the same checks cover a document
    that arrived as a value rather than as a file.
    """
    where = str(path) if path else "document"
    if raw is None:
        raise CliError(f"{where} is empty", exit_code=EXIT_USAGE)
    if not isinstance(raw, Mapping):
        raise CliError(
            f"{where} must be a mapping at the top level, not {type(raw).__name__}",
            exit_code=EXIT_USAGE,
        )

    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        raise CliError(
            f"{where} has unknown top-level keys: {', '.join(unknown)}",
            exit_code=EXIT_USAGE,
            details={"known_keys": sorted(_TOP_LEVEL_KEYS)},
        )

    version = raw.get("version")
    if version != DOCUMENT_VERSION:
        raise CliError(
            f"{where} declares version {version!r}; this CLI reads version {DOCUMENT_VERSION}",
            exit_code=EXIT_USAGE,
        )

    return Document(
        version=DOCUMENT_VERSION,
        environments=_parse_resources(raw.get("environments"), KIND_ENVIRONMENT, where),
        agents=_parse_resources(raw.get("agents"), KIND_AGENT, where),
        path=path,
    )


def _parse_resources(raw: Any, kind: str, where: str) -> tuple[ResourceSpec, ...]:
    """Validate one resource list and its per-item identity."""
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise CliError(
            f"{where}: {kind}s must be a list, not {type(raw).__name__}",
            exit_code=EXIT_USAGE,
        )

    specs: list[ResourceSpec] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise CliError(
                f"{where}: {kind}s[{index}] must be a mapping, not {type(item).__name__}",
                exit_code=EXIT_USAGE,
            )
        name = str(item.get("name") or "").strip()
        if not name:
            raise CliError(
                f"{where}: {kind}s[{index}] has no name",
                exit_code=EXIT_USAGE,
            )
        if name in seen:
            raise CliError(
                f"{where}: {kind} {name!r} is declared twice "
                f"(items {seen[name]} and {index})",
                exit_code=EXIT_USAGE,
            )
        seen[name] = index
        specs.append(ResourceSpec(kind=kind, name=name, fields=dict(item)))
    return tuple(specs)


__all__ = [
    "DOCUMENT_VERSION",
    "Document",
    "KIND_AGENT",
    "KIND_ENVIRONMENT",
    "ResourceSpec",
    "load_document",
    "parse_document",
]
