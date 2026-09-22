"""``astrabox init`` — start an ``astrabox.yaml``, or export the one a running
deployment already implies.

The export is driven entirely by the deployment's own field schema: for each
field the schema declares, the value is read from the stored document at that
field's ``path`` and written under its ``key``. Nothing about which fields are
writable, or where they live in the stored document, is decided here — so a
deployment that adds a field exports it without a change to this file.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrabox.cli.client import ApiClient, as_items, resolve_endpoint
from astrabox.cli.document import DOCUMENT_VERSION, KIND_AGENT, KIND_ENVIRONMENT
from astrabox.cli.flags import connection_flags
from astrabox.cli.output import (
    EXIT_CONFLICT,
    EXIT_OK,
    EXIT_USAGE,
    FORMAT_TABLE,
    CliError,
    emit,
)
from astrabox.cli.resources import schema_field_paths, walk_path

#: Default document name, matching what `apply -f` is usually pointed at.
DEFAULT_DOCUMENT_NAME = "astrabox.yaml"

#: Collections the export reads, in the order the document declares them.
_EXPORT_SOURCES = (
    (KIND_ENVIRONMENT, "environments", "/api/v1/admin/environments"),
    (KIND_AGENT, "agents", "/api/v1/agents"),
)

_SKELETON = """\
# An AstraBox deployment, as code.
#
# The field names below are the deployment's, not this file's. Read them with:
#   astrabox schema environment
#   astrabox schema agent
#
# Then converge a deployment onto this file:
#   astrabox diff  -f astrabox.yaml     # what would change
#   astrabox apply -f astrabox.yaml     # make it so
#
# `astrabox init --from-deployment` replaces this skeleton with what a running
# deployment already holds.
version: 1

environments:
  - name: default
    # Candidate sets for both enums come from what this deployment has
    # installed: `astrabox schema environment -o json`.
    engine_kind: claude_code
    endpoint_provider: litellm
    enabled: true

agents:
  - name: my-agent
    # Models the environment can serve: `astrabox get environments -o json`.
    model: claude-opus-5
    environment_name: default
    system: |
      You are a helpful agent.
    enabled: true
"""


def register(subparsers: Any) -> None:
    """Add ``astrabox init`` to the top-level parser."""
    init = subparsers.add_parser(
        "init",
        parents=[connection_flags()],
        help="Write a starting astrabox.yaml, or export a running deployment's.",
        description=(
            "Without --from-deployment this writes an annotated skeleton and "
            "contacts nothing. With it, the environments and Agents a "
            "deployment already holds are projected through its own field "
            "schema into a document `astrabox apply` accepts. An ambiguous "
            "Agent name refuses the export before a file is written."
        ),
    )
    init.add_argument(
        "-f",
        "--file",
        default=DEFAULT_DOCUMENT_NAME,
        help="Where to write the document (default: %(default)s).",
    )
    init.add_argument(
        "--from-deployment",
        action="store_true",
        help="Export what a running deployment holds instead of a skeleton.",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the file if it already exists.",
    )
    init.set_defaults(func=_cmd_init)


def _cmd_init(args: Any) -> int:
    """Handle ``astrabox init``."""
    target = Path(args.file)
    if target.exists() and not args.force:
        raise CliError(
            f"{target} already exists; pass --force to overwrite it",
            exit_code=EXIT_USAGE,
        )

    if args.from_deployment:
        client = ApiClient(resolve_endpoint(endpoint=args.endpoint, token=args.token))
        with client:
            document = export_document(client)
        body = _to_yaml(document)
        counts = {name: len(document.get(name, [])) for _, name, _ in _EXPORT_SOURCES}
    else:
        body = _SKELETON
        counts = {"environments": 1, "agents": 1}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")

    payload: dict[str, Any] = {"file": str(target), "from_deployment": bool(args.from_deployment)}
    payload.update(counts)
    emit(
        payload,
        output=args.output,
        note=f"wrote {target}" if args.output == FORMAT_TABLE else None,
    )
    return EXIT_OK


def export_document(client: ApiClient) -> dict[str, Any]:
    """Project a deployment's environments and Agents into a document body.

    Only fields the deployment's schema declares survive, so server-managed
    state — ids, versions, ownership, timestamps — never reaches the file and
    cannot be sent back on the next apply, where the write path would refuse
    it as an unknown field.

    An environment's stored secret is returned masked. Exporting the mask is
    the correct round trip: sending it back unmodified is how the write path is
    told to keep the secret it already holds.
    """
    document: dict[str, Any] = {"version": DOCUMENT_VERSION}
    for kind, key, route in _EXPORT_SOURCES:
        paths = schema_field_paths(client, kind)
        items = as_items(client.get(route))
        if kind == KIND_AGENT:
            _refuse_ambiguous_agent_names(items)
        document[key] = [_project(item, paths) for item in items]
    return document


def _refuse_ambiguous_agent_names(items: list[Mapping[str, Any]]) -> None:
    """Stop before exporting Agents a name-keyed document cannot identify."""
    by_name: dict[str, list[str]] = {}
    for item in items:
        name = str(item.get("name") or "").strip()
        if name:
            by_name.setdefault(name, []).append(str(item.get("agent_id") or ""))
    conflicts = [
        {"name": name, "agent_ids": sorted(agent_ids)}
        for name, agent_ids in sorted(by_name.items())
        if len(agent_ids) > 1
    ]
    if conflicts:
        names = ", ".join(repr(item["name"]) for item in conflicts)
        raise CliError(
            f"cannot export deployment: ambiguous Agent names: {names}",
            exit_code=EXIT_CONFLICT,
            details={"conflicts": conflicts},
        )


def _project(stored: Mapping[str, Any], paths: Mapping[str, str]) -> dict[str, Any]:
    """Read one stored document through the schema's key-to-path map.

    Only fields the schema declares survive, so server-managed state — ids,
    versions, ownership, timestamps — never reaches the file and cannot be sent
    back on the next apply, where the write path would refuse it.
    """
    projected: dict[str, Any] = {}
    for key, path in paths.items():
        value = walk_path(stored, path)
        if value is not None:
            projected[key] = value
    return projected


def _to_yaml(document: Mapping[str, Any]) -> str:
    """Serialise a document with its declaration order preserved."""
    import yaml

    return str(
        yaml.safe_dump(dict(document), sort_keys=False, allow_unicode=True, width=100)
    )


__all__ = ["DEFAULT_DOCUMENT_NAME", "export_document", "register"]
