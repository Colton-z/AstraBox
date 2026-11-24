"""``astrabox assistant`` — configure one of your Assistants and run its workspace.

Each subcommand sends the request the console's Assistant page sends for the
same action (``frontend/src/assistant/api.ts``), so the console and the CLI
stay one path to one service:

* ``show`` reads ``GET /api/v1/assistants/{id}``, the record with its
  workspace's state.
* ``set`` changes the Assistant's own model and its Agent program's
  configuration blocks with ``PATCH /api/v1/assistants/{id}``. A block goes into
  ``engine_options`` under the key the program declares and with the program's
  own field names, exactly as the console's per-block JSON field writes it
  (``buildAssistantExtensionSection`` in
  ``frontend/src/manage/assistantEditConfig.ts``): the CLI reads the stored
  bag, replaces only the named blocks, and sends the whole bag back.
* ``secret list`` and ``secret set`` report and write the credentials the
  Assistant's runtime asks its owner for (``runtime.owner_secrets``) with
  ``PUT /api/v1/assistants/{id}/owner-secrets/{key}``. The value is read from
  a prompt that does not echo it, from standard input, or from a file — never
  from the command line, where it would stay in the shell's history — and no
  output carries it: the deployment reports only whether a value is set.
* ``hibernate`` and ``wake`` are the console's Pause workspace and Resume
  workspace (``POST .../workspace/hibernate`` and ``.../workspace/wake``).

An Assistant is named by its ``assistant_id`` or its display name, matched
among the caller's own Assistants. A name two of them share is refused with the
ids it matched rather than resolved to either.
"""

from __future__ import annotations

import getpass
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from astrabox.cli.client import ApiClient, as_items, resolve_endpoint
from astrabox.cli.flags import connection_flags
from astrabox.cli.output import EXIT_CONFLICT, EXIT_FAILED, EXIT_OK, EXIT_USAGE, CliError, emit

_ASSISTANTS = "/api/v1/assistants"

_SECRET_COLUMNS = ("key", "configured", "deployment_default", "required", "updated_at", "label")
_HIBERNATE_COLUMNS = (
    "assistant_id", "hibernated", "released", "recovery_required", "sandbox_id", "hibernated_at",
)
_WAKE_COLUMNS = (
    "assistant_id", "state", "current_sandbox_id", "provisioning_session_id",
    "recovery_pending_sandbox_id", "retryable",
)

#: The record's members ``show`` and ``set`` render as rows, in this order.
#: ``engine_options`` is expanded to one row per configuration block.
_RECORD_FIELDS = (
    "assistant_id",
    "display_name",
    "engine_kind",
    "runtime_profile_name",
    "model_config_override",
    "engine_options",
    "workspace_state",
    "current_sandbox_id",
    "workspace_action",
    "workspace_error",
    "main_session_id",
)


def register(subparsers: Any) -> None:
    """Add ``astrabox assistant`` to the top-level parser."""
    group = subparsers.add_parser(
        "assistant",
        help="Configure one of your Assistants and pause or resume its workspace.",
        description=(
            "Read and change one of your Assistants: its model, its Agent "
            "program's configuration, the credentials its runtime asks you for, "
            "and its workspace. `astrabox get assistants` lists them."
        ),
    )
    commands = group.add_subparsers(dest="assistant_command", required=True)

    show = commands.add_parser(
        "show",
        parents=[connection_flags()],
        help="Show an Assistant's configuration and its workspace's state.",
        description=(
            "Read the Assistant's stored configuration and its workspace's state. "
            "--output json prints the whole record."
        ),
    )
    show.add_argument("assistant", help="The Assistant's id, or its display name.")
    show.set_defaults(func=_cmd_show)

    update = commands.add_parser(
        "set",
        parents=[connection_flags()],
        help="Change an Assistant's model or its Agent program's configuration.",
        description=(
            "Change the Assistant's own model, or replace one of the configuration "
            "blocks its Agent program declares. The deployment checks every value "
            "against the program before it stores it, and prepares a running "
            "workspace again with the new revision."
        ),
    )
    update.add_argument("assistant", help="The Assistant's id, or its display name.")
    model = update.add_mutually_exclusive_group()
    model.add_argument(
        "--model",
        help="The model this Assistant uses, a model name its model connection offers.",
    )
    model.add_argument(
        "--inherit-model",
        action="store_true",
        help="Drop the Assistant's own model and use the one the Assistant defaults name.",
    )
    update.add_argument(
        "--engine-option",
        action="append",
        default=[],
        metavar="KEY=JSON",
        help=(
            "Replace the configuration block KEY with the JSON value; `null` removes "
            "the block. KEY and the value's fields are the Agent program's own, for "
            'example default_model=\'{"reasoningEffort": "xhigh"}\'. Repeatable.'
        ),
    )
    update.set_defaults(func=_cmd_set)

    secret = commands.add_parser(
        "secret",
        help="List or set the credentials an Assistant's runtime asks you for.",
        description=(
            "Each credential is write-only: the deployment keeps it in its secret "
            "store and reports only whether it is set."
        ),
    )
    secret_commands = secret.add_subparsers(dest="assistant_secret_command", required=True)

    secret_list = secret_commands.add_parser(
        "list",
        parents=[connection_flags()],
        help="List the credentials the runtime declares and whether each is set.",
        description=(
            "List the credentials the Assistant's runtime declares and whether each is set."
        ),
    )
    secret_list.add_argument("assistant", help="The Assistant's id, or its display name.")
    secret_list.set_defaults(func=_cmd_secret_list)

    secret_set = secret_commands.add_parser(
        "set",
        parents=[connection_flags()],
        help="Set or replace one credential.",
        description=(
            "Read the value from the terminal without echoing it, from standard "
            "input when that is not a terminal, or from --from-file, and store it "
            "as the Assistant's credential KEY, replacing any earlier value."
        ),
    )
    secret_set.add_argument("assistant", help="The Assistant's id, or its display name.")
    secret_set.add_argument("key", help="The credential's key, as `secret list` shows it.")
    secret_set.add_argument(
        "--from-file",
        metavar="PATH",
        help="Read the value from this file instead of the terminal or standard input.",
    )
    secret_set.set_defaults(func=_cmd_secret_set)

    hibernate = commands.add_parser(
        "hibernate",
        parents=[connection_flags()],
        help="Pause the Assistant's workspace: save its state and release its sandbox.",
        description=(
            "Save the Agent program's native state in the deployment's database and "
            "release the workspace's sandbox. Exits 1 when the deployment could not "
            "confirm both; run it again."
        ),
    )
    hibernate.add_argument("assistant", help="The Assistant's id, or its display name.")
    hibernate.set_defaults(func=_cmd_hibernate)

    wake = commands.add_parser(
        "wake",
        parents=[connection_flags()],
        help="Resume the Assistant's workspace in a new sandbox.",
        description=(
            "Ask the deployment to make the workspace ready, restoring the saved "
            "state into a new sandbox. It answers once the request is accepted; "
            "`show` reports when the workspace is READY."
        ),
    )
    wake.add_argument("assistant", help="The Assistant's id, or its display name.")
    wake.set_defaults(func=_cmd_wake)


def resolve_assistant(client: ApiClient, wanted: str) -> Mapping[str, Any]:
    """The caller's one Assistant whose id or display name is ``wanted``.

    The list carries only the caller's own Assistants, so another owner's id
    resolves to nothing, as the deployment answers for it. Display names are
    not unique; a name several Assistants share stops with their ids.
    """
    matches = [
        item
        for item in as_items(client.get(_ASSISTANTS))
        if wanted
        in (str(item.get("assistant_id") or ""), str(item.get("display_name") or "").strip())
    ]
    if len(matches) > 1:
        raise CliError(
            f"{wanted!r} names {len(matches)} of your Assistants; name one by its id",
            exit_code=EXIT_CONFLICT,
            details={
                "assistant_ids": sorted(str(item.get("assistant_id") or "") for item in matches)
            },
        )
    if not matches:
        raise CliError(
            f"you have no Assistant {wanted!r} on this deployment", exit_code=EXIT_FAILED
        )
    return matches[0]


def _client(args: Any) -> ApiClient:
    return ApiClient(resolve_endpoint(endpoint=args.endpoint, token=args.token))


def _cmd_show(args: Any) -> int:
    """Handle ``astrabox assistant show``."""
    with _client(args) as client:
        assistant_id = str(resolve_assistant(client, args.assistant)["assistant_id"])
        record = client.get(f"{_ASSISTANTS}/{assistant_id}")
    emit(record, output=args.output, table=_record_rows(record), columns=("field", "value"))
    return EXIT_OK


def _cmd_set(args: Any) -> int:
    """Handle ``astrabox assistant set``."""
    blocks = _engine_option_blocks(args.engine_option)
    if args.model is None and not args.inherit_model and not blocks:
        raise CliError(
            "nothing to set: give --model, --inherit-model or --engine-option",
            exit_code=EXIT_USAGE,
        )
    with _client(args) as client:
        stored = resolve_assistant(client, args.assistant)
        body: dict[str, Any] = {}
        if args.model is not None:
            body["model_config_override"] = {"model_name": args.model}
        elif args.inherit_model:
            body["model_config_override"] = None
        if blocks:
            bag = dict(stored.get("engine_options") or {})
            for key, value in blocks.items():
                if value is None:
                    bag.pop(key, None)
                else:
                    bag[key] = value
            body["engine_options"] = bag
        record = client.patch(f"{_ASSISTANTS}/{stored['assistant_id']}", json=body)
    emit(record, output=args.output, table=_record_rows(record), columns=("field", "value"))
    return EXIT_OK


def _cmd_secret_list(args: Any) -> int:
    """Handle ``astrabox assistant secret list``."""
    with _client(args) as client:
        assistant_id = str(resolve_assistant(client, args.assistant)["assistant_id"])
        record = client.get(f"{_ASSISTANTS}/{assistant_id}")
    _emit_secrets(record, output=args.output)
    return EXIT_OK


def _cmd_secret_set(args: Any) -> int:
    """Handle ``astrabox assistant secret set``."""
    with _client(args) as client:
        assistant_id = str(resolve_assistant(client, args.assistant)["assistant_id"])
        value = _read_secret(args.key, from_file=args.from_file)
        record = client.put(
            f"{_ASSISTANTS}/{assistant_id}/owner-secrets/{args.key}", json={"value": value}
        )
    _emit_secrets(record, output=args.output)
    return EXIT_OK


def _cmd_hibernate(args: Any) -> int:
    """Handle ``astrabox assistant hibernate``."""
    with _client(args) as client:
        assistant_id = str(resolve_assistant(client, args.assistant)["assistant_id"])
        outcome = client.post(f"{_ASSISTANTS}/{assistant_id}/workspace/hibernate")
    if not isinstance(outcome, Mapping) or outcome.get("hibernated") is not True:
        # The deployment answers 200 when it could not confirm the save and the
        # release; a script that runs `wake` next must not read that as paused.
        raise CliError(
            f"the workspace of Assistant {assistant_id} was not hibernated; run hibernate again",
            exit_code=EXIT_FAILED,
            details=dict(outcome) if isinstance(outcome, Mapping) else {},
        )
    emit(outcome, output=args.output, table=[outcome], columns=_HIBERNATE_COLUMNS)
    return EXIT_OK


def _cmd_wake(args: Any) -> int:
    """Handle ``astrabox assistant wake``."""
    with _client(args) as client:
        assistant_id = str(resolve_assistant(client, args.assistant)["assistant_id"])
        posture = client.post(f"{_ASSISTANTS}/{assistant_id}/workspace/wake")
    emit(posture, output=args.output, table=[posture], columns=_WAKE_COLUMNS)
    return EXIT_OK


def _engine_option_blocks(raw: Sequence[str]) -> dict[str, Any]:
    """Parse repeated ``KEY=JSON`` arguments, refusing a malformed or repeated key."""
    blocks: dict[str, Any] = {}
    for item in raw:
        key, separator, text = item.partition("=")
        key = key.strip()
        if not separator or not key:
            raise CliError(f"--engine-option {item!r} is not KEY=JSON", exit_code=EXIT_USAGE)
        if key in blocks:
            raise CliError(f"--engine-option {key!r} is given twice", exit_code=EXIT_USAGE)
        try:
            blocks[key] = json.loads(text)
        except ValueError as exc:
            raise CliError(
                f"--engine-option {key!r} is not valid JSON: {exc}", exit_code=EXIT_USAGE
            ) from exc
    return blocks


def _read_secret(key: str, *, from_file: str | None) -> str:
    """The value from ``from_file``, a prompt that does not echo it, or a piped stdin.

    The whole input is read and its surrounding whitespace dropped, which is
    what the deployment stores; an empty value is refused before any request.
    """
    if from_file:
        try:
            value = Path(from_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise CliError(
                f"cannot read {from_file}: {exc.strerror}", exit_code=EXIT_USAGE
            ) from exc
    elif sys.stdin.isatty():
        value = getpass.getpass(f"{key}: ")
    else:
        value = sys.stdin.read()
    value = value.strip()
    if not value:
        raise CliError(f"no value was given for {key}", exit_code=EXIT_USAGE)
    return value


def _emit_secrets(record: Any, *, output: str) -> None:
    """Report the runtime's declared credentials: whether each is set, never a value."""
    runtime = record.get("runtime") if isinstance(record, Mapping) else None
    secrets = list((runtime or {}).get("owner_secrets") or [])
    payload = {
        "assistant_id": record.get("assistant_id") if isinstance(record, Mapping) else None,
        "owner_secrets": secrets,
    }
    emit(payload, output=output, table=secrets, columns=_SECRET_COLUMNS)


def _record_rows(record: Any) -> list[dict[str, Any]]:
    """One ``field``/``value`` row per member the record carries.

    Nested members print as compact JSON, so a configuration block reads back
    in the form ``set`` takes it. A member the answer does not carry has no row:
    the answer to ``set`` has no workspace members, and an empty row would read
    as a workspace without a state.
    """
    if not isinstance(record, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for name in _RECORD_FIELDS:
        if name not in record:
            continue
        value = record[name]
        if name == "engine_options" and isinstance(value, Mapping) and value:
            rows.extend(
                {"field": f"engine_options.{key}", "value": _compact(block)}
                for key, block in sorted(value.items())
            )
            continue
        rows.append({"field": name, "value": _compact(value)})
    return rows


def _compact(value: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return value


__all__ = ["register", "resolve_assistant"]
