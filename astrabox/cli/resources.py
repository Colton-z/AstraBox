"""The client subcommands: read a deployment's contracts, list what it holds,
and converge a declarative document against it.

These talk to a running deployment over HTTP (:mod:`astrabox.cli.client`); none
of them import the runtime. The operator commands that boot a deployment live
in :mod:`astrabox.cli.serve`.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from typing import Any

from astrabox.cli.client import ApiClient, as_items, resolve_endpoint
from astrabox.cli.flags import connection_flags
from astrabox.cli.document import (
    KIND_AGENT,
    KIND_ENVIRONMENT,
    Document,
    ResourceSpec,
    load_document,
)
from astrabox.cli.output import (
    EXIT_CONFLICT,
    EXIT_FAILED,
    EXIT_OK,
    FORMAT_JSON,
    CliError,
    emit,
)

#: Schema kinds `astrabox schema` serves, and the route each is read from.
SCHEMA_ROUTES = {
    "agent": "/api/v1/admin/agent-schema",
    "environment": "/api/v1/admin/environment-schema",
}

#: Collections `astrabox get` lists, with the route and the columns the table
#: format shows. `--output json` always carries the full documents.
GET_ROUTES: dict[str, tuple[str, tuple[str, ...]]] = {
    "agents": ("/api/v1/agents", ("name", "model", "environment_name", "enabled", "agent_id")),
    "environments": ("/api/v1/admin/environments", ("name", "enabled", "endpoint_provider")),
    "assistants": (
        "/api/v1/assistants",
        ("display_name", "assistant_id", "engine_kind", "environment_name"),
    ),
    "sessions": (
        "/api/v1/sessions",
        ("session_id", "title", "agent_id", "state", "created_at"),
    ),
    "mcp-servers": ("/api/v1/admin/mcp-servers", ("name", "mcp_server_id", "transport")),
}

#: Per-resource verdicts an apply or diff reports.
ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_UNCHANGED = "unchanged"
ACTION_DELETE = "delete"
ACTION_RETAINED = "retained"
ACTION_ABSENT = "absent"


def register(subparsers: Any) -> None:
    """Add the client subcommands to the top-level ``astrabox`` parser."""
    common = connection_flags()

    schema = subparsers.add_parser(
        "schema",
        parents=[common],
        help="Print the deployment's field contract for a resource kind.",
        description=(
            "Read the authoring schema the deployment enforces on write: every "
            "field's key, type, whether it is required, and its candidate set "
            "where it has one. This is the authoritative answer to what may "
            "appear in an astrabox.yaml resource."
        ),
    )
    schema.add_argument("kind", choices=sorted(SCHEMA_ROUTES))
    schema.set_defaults(func=_cmd_schema)

    get = subparsers.add_parser(
        "get",
        parents=[common],
        help="List what a deployment holds.",
        description="List a collection, or one member of it by name or id.",
    )
    get.add_argument("kind", choices=sorted(GET_ROUTES))
    get.add_argument(
        "name",
        nargs="?",
        help="Show one member, matched on name and then on id.",
    )
    get.set_defaults(func=_cmd_get)

    apply_parser = subparsers.add_parser(
        "apply",
        parents=[common],
        help="Converge a deployment onto an astrabox.yaml document.",
        description=(
            "Create or update every resource the document declares. Resources "
            "the document does not mention are left alone — removing them is "
            "`astrabox destroy`."
        ),
    )
    apply_parser.add_argument("-f", "--file", required=True, help="Path to astrabox.yaml.")
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without sending any write.",
    )
    apply_parser.set_defaults(func=_cmd_apply)

    diff = subparsers.add_parser(
        "diff",
        parents=[common],
        help="Report what `apply` would change, sending no writes.",
        description="The same reads apply performs, reported without the writes.",
    )
    diff.add_argument("-f", "--file", required=True, help="Path to astrabox.yaml.")
    diff.set_defaults(func=_cmd_diff)

    destroy = subparsers.add_parser(
        "destroy",
        parents=[common],
        help="Remove the resources an astrabox.yaml document declares.",
        description=(
            "Delete every Agent the document declares. Environments are "
            "reported as retained: the deployment serves no delete route for "
            "them."
        ),
    )
    destroy.add_argument("-f", "--file", required=True, help="Path to astrabox.yaml.")
    destroy.add_argument(
        "--yes",
        action="store_true",
        required=True,
        help="Required acknowledgement that this deletes live resources.",
    )
    destroy.set_defaults(func=_cmd_destroy)


def _client(args: argparse.Namespace) -> ApiClient:
    """Build the API client the subcommand's flags and env vars resolve to."""
    return ApiClient(resolve_endpoint(endpoint=args.endpoint, token=args.token))


def _cmd_schema(args: argparse.Namespace) -> int:
    """Handle ``astrabox schema <kind>``."""
    with _client(args) as client:
        payload = client.get(SCHEMA_ROUTES[args.kind])
    fields = payload.get("fields") if isinstance(payload, Mapping) else None
    if args.output == FORMAT_JSON or not isinstance(fields, list):
        emit(payload, output=args.output)
        return EXIT_OK
    rows = [
        {
            "key": field.get("key", ""),
            "type": field.get("type", ""),
            "required": bool(field.get("required")),
            "enum": field.get("enum") or "",
        }
        for field in fields
        if isinstance(field, Mapping)
    ]
    emit(payload, output=args.output, table=rows, columns=("key", "type", "required", "enum"))
    return EXIT_OK


def _cmd_get(args: argparse.Namespace) -> int:
    """Handle ``astrabox get <kind> [name]``."""
    route, columns = GET_ROUTES[args.kind]
    with _client(args) as client:
        payload = client.get(route)
    items = as_items(payload)
    if args.name:
        items = [item for item in items if _identifies(item, args.name)]
        if not items:
            raise CliError(
                f"no {args.kind} named {args.name!r} on this deployment",
                exit_code=EXIT_FAILED,
            )
    emit(
        items if not args.name else items[0] if len(items) == 1 else items,
        output=args.output,
        table=list(items),
        columns=columns,
    )
    return EXIT_OK


def _cmd_apply(args: argparse.Namespace) -> int:
    """Handle ``astrabox apply -f <document>``."""
    document = load_document(args.file)
    with _client(args) as client:
        results = plan_and_run(client, document, write=not args.dry_run)
    emit(
        {"dry_run": bool(args.dry_run), "resources": results},
        output=args.output,
        table=results,
        columns=("kind", "name", "action", "fields"),
    )
    return EXIT_OK


def _cmd_diff(args: argparse.Namespace) -> int:
    """Handle ``astrabox diff -f <document>``."""
    document = load_document(args.file)
    with _client(args) as client:
        results = plan_and_run(client, document, write=False)
    emit(
        {"dry_run": True, "resources": results},
        output=args.output,
        table=results,
        columns=("kind", "name", "action", "fields"),
    )
    return EXIT_OK


def _cmd_destroy(args: argparse.Namespace) -> int:
    """Handle ``astrabox destroy -f <document> --yes``."""
    document = load_document(args.file)
    results: list[dict[str, Any]] = []
    with _client(args) as client:
        for spec in document.agents:
            existing = match_agent(client, spec.name)
            if existing is None:
                results.append(_result(spec, ACTION_ABSENT, ()))
                continue
            client.delete(f"/api/v1/agents/{existing['agent_id']}")
            results.append(_result(spec, ACTION_DELETE, ()))
        for spec in document.environments:
            # The deployment serves GET and PUT for an environment preset and
            # no DELETE, so destroy reports the environment as kept rather
            # than claiming a removal it did not perform.
            results.append(_result(spec, ACTION_RETAINED, ()))
    emit(
        {"resources": results},
        output=args.output,
        table=results,
        columns=("kind", "name", "action", "fields"),
    )
    return EXIT_OK


def schema_field_paths(client: ApiClient, kind: str) -> dict[str, str]:
    """Where each writable field of a kind lives in the stored document.

    The document names a field by its schema ``key``; the deployment may store
    it somewhere else, which the schema states as that field's ``path`` (an
    Agent's display fields live beneath ``display_meta``). Comparing a declared
    value against the stored document therefore has to follow the path — a
    flat lookup finds nothing there and reports the field as changed on every
    run.
    """
    schema = client.get(SCHEMA_ROUTES[kind])
    fields = schema.get("fields") if isinstance(schema, Mapping) else None
    if not isinstance(fields, list):
        raise CliError(f"{kind} schema carried no field list")
    paths = {
        str(field["key"]): str(field.get("path") or field["key"])
        for field in fields
        if isinstance(field, Mapping) and str(field.get("key") or "").strip()
    }
    if not paths:
        raise CliError(f"{kind} schema declared no fields")
    return paths


def walk_path(document: Mapping[str, Any] | None, path: str) -> Any:
    """Follow a dotted path through a stored document, or return ``None``."""
    node: Any = document
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def plan_and_run(client: ApiClient, document: Document, *, write: bool) -> list[dict[str, Any]]:
    """Compute each resource's verdict, performing the writes when asked.

    Environments are processed before Agents because an Agent's
    ``environment_name`` is validated against an existing environment: applying
    them in document order would fail a first-run document that declares both.
    """
    results: list[dict[str, Any]] = []
    if document.environments:
        paths = schema_field_paths(client, KIND_ENVIRONMENT)
        for spec in document.environments:
            results.append(_apply_environment(client, spec, paths=paths, write=write))
    if document.agents:
        paths = schema_field_paths(client, KIND_AGENT)
        for spec in document.agents:
            results.append(_apply_agent(client, spec, paths=paths, write=write))
    return results


def _apply_environment(
    client: ApiClient, spec: ResourceSpec, *, paths: Mapping[str, str], write: bool
) -> dict[str, Any]:
    """Create or update one environment preset, keyed by name.

    ``PUT /api/v1/admin/environments/{name}`` upserts, so the write is the same
    call in both cases and applying an unchanged document converges.
    """
    existing = _find_named(client.get("/api/v1/admin/environments"), spec.name)
    changed = _changed_fields(spec.fields, existing, paths)
    action = _verdict(existing, changed)
    if write and action != ACTION_UNCHANGED:
        body = {key: value for key, value in spec.fields.items() if key != "name"}
        client.put(f"/api/v1/admin/environments/{spec.name}", json=body)
    return _result(spec, action, changed)


def _apply_agent(
    client: ApiClient, spec: ResourceSpec, *, paths: Mapping[str, str], write: bool
) -> dict[str, Any]:
    """Create or update one Agent, keyed by name.

    The stored ``version`` is sent with every update so a document written
    against a stale read is refused with a conflict rather than overwriting a
    concurrent edit.
    """
    existing = match_agent(client, spec.name)
    changed = _changed_fields(spec.fields, existing, paths)
    action = _verdict(existing, changed)
    if write and action == ACTION_CREATE:
        client.post("/api/v1/agents", json=dict(spec.fields))
    elif write and action == ACTION_UPDATE:
        assert existing is not None
        body = dict(spec.fields)
        stored_version = existing.get("version")
        if stored_version is not None:
            body["version"] = stored_version
        client.put(f"/api/v1/agents/{existing['agent_id']}", json=body)
    return _result(spec, action, changed)


def match_agent(client: ApiClient, name: str) -> Mapping[str, Any] | None:
    """Find the one Agent with this name, or refuse an ambiguous match.

    The deployment does not constrain Agent names to be unique. Two Agents
    sharing a name make the document ambiguous, and choosing one of them would
    make the same document mean different things on two deployments — so this
    stops and names the ids it found.
    """
    matches = [
        item
        for item in as_items(client.get("/api/v1/agents"))
        if str(item.get("name") or "").strip() == name
    ]
    if len(matches) > 1:
        raise CliError(
            f"agent name {name!r} matches {len(matches)} agents on this deployment",
            exit_code=EXIT_CONFLICT,
            details={"agent_ids": sorted(str(item.get("agent_id") or "") for item in matches)},
        )
    return matches[0] if matches else None


def _changed_fields(
    desired: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
    paths: Mapping[str, str],
) -> tuple[str, ...]:
    """Field keys whose declared value differs from what the deployment holds.

    Each key is read from the stored document at the schema's ``path`` for it,
    not at the key itself: the two differ wherever the deployment nests a field
    (``display_name`` is stored at ``display_meta.display_name``), and a flat
    lookup would find nothing and call every such field changed forever.

    Only keys the document declares are compared. A field the document omits is
    one the deployment's stored value or its own default decides, so its
    absence is never a change.

    An environment's stored secret comes back masked, so a document carrying
    the real value reports that field as changed on every run. The write is
    still idempotent — the deployment keeps the stored secret when the mask is
    sent back unmodified.
    """
    if existing is None:
        return tuple(sorted(key for key in desired if key != "name"))
    return tuple(
        sorted(
            key
            for key, value in desired.items()
            if key != "name" and walk_path(existing, paths.get(key, key)) != value
        )
    )


def _verdict(existing: Mapping[str, Any] | None, changed: Sequence[str]) -> str:
    """Turn a lookup and a field delta into the reported action."""
    if existing is None:
        return ACTION_CREATE
    return ACTION_UPDATE if changed else ACTION_UNCHANGED


def _result(spec: ResourceSpec, action: str, changed: Sequence[str]) -> dict[str, Any]:
    """One row of an apply/diff/destroy report."""
    return {
        "kind": spec.kind,
        "name": spec.name,
        "action": action,
        "fields": list(changed),
    }


def _find_named(payload: Any, name: str) -> Mapping[str, Any] | None:
    """The first member of a collection whose ``name`` matches."""
    for item in as_items(payload):
        if str(item.get("name") or "").strip() == name:
            return item
    return None


def _identifies(item: Mapping[str, Any], wanted: str) -> bool:
    """Whether a listed item is the one the caller named.

    The name field first, then any ``*_id`` field, so ``get agents web`` and
    ``get agents <uuid>`` both resolve. Two name fields are checked because the
    collections carry different ones: an Agent and an environment preset have
    ``name``, while an Assistant's catalog row has ``display_name``
    (``astrabox/api/routes/assistant.py``).
    """
    for key in ("name", "display_name"):
        if str(item.get(key) or "").strip() == wanted:
            return True
    return any(
        key.endswith("_id") and str(value or "").strip() == wanted
        for key, value in item.items()
    )


__all__ = [
    "GET_ROUTES",
    "SCHEMA_ROUTES",
    "match_agent",
    "plan_and_run",
    "register",
    "schema_field_paths",
    "walk_path",
]
