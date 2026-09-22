"""Exit codes, the failure type, and the two output formats.

Every client subcommand renders through :func:`emit` and fails through
:class:`CliError`, so a caller sees one result shape per format and one exit
code per kind of failure regardless of which command it ran.

Both formats are explicit choices, never inferred from whether stdout is a
terminal. A command whose output shape changes when it is piped cannot be
scripted against, and scripting is what this surface is for.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: The operation succeeded.
EXIT_OK = 0
#: The operation was rejected by the deployment, or the resource is absent.
EXIT_FAILED = 1
#: The invocation itself is wrong: bad flags, or an unreadable/invalid document.
EXIT_USAGE = 2
#: The deployment refused the credential, or the credential lacks the scope.
EXIT_AUTH = 3
#: The endpoint could not be reached at all.
EXIT_UNREACHABLE = 4
#: Apply cannot proceed without guessing — an ambiguous name or a stale version.
EXIT_CONFLICT = 5

#: Output formats accepted by ``--output``.
FORMAT_TABLE = "table"
FORMAT_JSON = "json"
OUTPUT_FORMATS = (FORMAT_TABLE, FORMAT_JSON)


class CliError(Exception):
    """A failure with the exit code a caller should branch on.

    ``code`` carries the deployment's own registered error code when the
    failure came from the API, so a caller reads one vocabulary rather than a
    CLI-local translation of it. It is ``None`` for failures raised before any
    request was sent.

    ``details`` is merged into the JSON failure object. Use it for facts the
    caller needs to act — the ids behind an ambiguous name, the field that
    failed validation — not for restating the message.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = EXIT_FAILED,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.code = code
        self.details = dict(details or {})


def emit(payload: Any, *, output: str, table: Sequence[Mapping[str, Any]] | None = None,
         columns: Sequence[str] | None = None, note: str | None = None) -> None:
    """Write one successful result in the requested format.

    ``payload`` is the JSON body. ``table``/``columns`` describe the same data
    for the human format; when ``table`` is omitted the JSON body is printed in
    both formats, which is right for a schema dump and wrong for a list.

    ``note`` is a human-facing line that carries no data — it goes to stderr so
    that piping table output stays free of prose.
    """
    if output == FORMAT_JSON:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        return
    if table is None:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(render_table(table, columns or _infer_columns(table)), end="")
    if note:
        print(note, file=sys.stderr)


def emit_failure(error: CliError, *, output: str) -> None:
    """Write one failure in the requested format.

    Under ``--output json`` the failure is a JSON object on stdout, not prose on
    stderr: a caller that parses stdout must not have to handle "sometimes JSON,
    sometimes a sentence".
    """
    if output == FORMAT_JSON:
        body: dict[str, Any] = {"ok": False, "error": error.message}
        if error.code:
            body["code"] = error.code
        body.update(error.details)
        print(json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False))
        return
    print(f"astrabox: {error.message}", file=sys.stderr)
    for key, value in sorted(error.details.items()):
        print(f"  {key}: {_cell(value)}", file=sys.stderr)


def render_table(rows: Iterable[Mapping[str, Any]], columns: Sequence[str]) -> str:
    """Render rows as space-aligned columns with an uppercase header.

    Columns are padded to their widest cell so the output stays readable in a
    terminal and remains splittable on runs of whitespace. An empty row set
    renders the header alone rather than nothing, so a caller can tell an empty
    list from a command that produced no output.
    """
    materialised = [{column: _cell(row.get(column)) for column in columns} for row in rows]
    widths = {
        column: max([len(column)] + [len(row[column]) for row in materialised])
        for column in columns
    }
    lines = ["  ".join(column.upper().ljust(widths[column]) for column in columns).rstrip()]
    for row in materialised:
        lines.append("  ".join(row[column].ljust(widths[column]) for column in columns).rstrip())
    return "\n".join(lines) + "\n"


def _infer_columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Take the column order from the first row, extended by any later keys."""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


def _cell(value: Any) -> str:
    """Flatten one value into a single table cell.

    Lists join with commas and mappings collapse to a count: a table row must
    stay on one line, and a nested document belongs in ``--output json``.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value.replace("\n", " ")
    if isinstance(value, Mapping):
        return f"{{{len(value)} keys}}"
    if isinstance(value, (list, tuple)):
        return ", ".join(_cell(item) for item in value)
    return str(value)


__all__ = [
    "CliError",
    "EXIT_AUTH",
    "EXIT_CONFLICT",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_UNREACHABLE",
    "EXIT_USAGE",
    "FORMAT_JSON",
    "FORMAT_TABLE",
    "OUTPUT_FORMATS",
    "emit",
    "emit_failure",
    "render_table",
]
