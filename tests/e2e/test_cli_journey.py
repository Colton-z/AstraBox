"""CLI e2e — the developer command surface against a live deployment.

Every case here runs the real console script in its own process, because the
exit code and stdout *are* the contract: a script or an assistant branches on
them, and calling ``main()`` in-process would prove neither.

The journey mirrors what an assistant configuring a deployment actually does:
read the field contract, read what is there, export it, prove the export is a
document apply accepts, then create and remove a resource of its own.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_cli_journey.py -m e2e -s
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

#: Exit codes the surface promises (astrabox/cli/output.py).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNREACHABLE = 4


class CliResult:
    """One completed CLI invocation."""

    def __init__(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.code = completed.returncode
        self.out = completed.stdout
        self.err = completed.stderr

    def json(self) -> Any:
        """stdout parsed as JSON, with the raw text in the failure message."""
        try:
            return json.loads(self.out)
        except ValueError as exc:  # pragma: no cover - reported, not handled
            raise AssertionError(f"stdout was not JSON: {self.out[:400]}") from exc


def _agent_environment(cli: Any) -> dict[str, Any]:
    """Choose an enabled Environment that advertises Agent Sessions."""
    environments = cli("get", "environments", "-o", "json").json()
    environment = next(
        (
            row
            for row in environments
            if row.get("enabled") is not False
            and "agent_chat" in row.get("supported_session_kinds", [])
        ),
        None,
    )
    assert environment is not None, "deployment offers no enabled Agent Environment"
    return environment


def _assistant_environment(cli: Any) -> dict[str, Any]:
    """Choose an enabled Environment that advertises only Assistant Sessions."""
    environments = cli("get", "environments", "-o", "json").json()
    environment = next(
        (
            row
            for row in environments
            if row.get("enabled") is not False
            and "assistant_chat" in row.get("supported_session_kinds", [])
            and "agent_chat" not in row.get("supported_session_kinds", [])
        ),
        None,
    )
    assert environment is not None, "deployment offers no Assistant-only Environment"
    return environment


@pytest.fixture(scope="session")
def cli(e2e_base_url: str) -> Any:
    """Invoke the installed CLI against the deployment under test."""
    token = ""
    raw = os.getenv("ASTRABOX_E2E_AUTH_TOKEN_FILE", "")
    if raw:
        token = Path(raw).read_text(encoding="utf-8").strip()

    def run(*args: str, expect: int | None = EXIT_OK) -> CliResult:
        env = {**os.environ, "ASTRABOX_ENDPOINT": e2e_base_url}
        if token:
            env["ASTRABOX_TOKEN"] = token
        else:
            env.pop("ASTRABOX_TOKEN", None)
        # The console script may not be on PATH in every harness; the module
        # entry point is the same code either way.
        completed = subprocess.run(
            [sys.executable, "-c", "import sys;from astrabox.cli import main;sys.exit(main(sys.argv[1:]))", *args],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        result = CliResult(completed)
        if expect is not None:
            assert result.code == expect, (
                f"astrabox {' '.join(args)} exited {result.code}, expected {expect}\n"
                f"stdout: {result.out[:500]}\nstderr: {result.err[:500]}"
            )
        return result

    return run


def test_status_reports_a_live_deployment(cli: Any) -> None:
    """The command a caller runs before anything else must agree with the
    deployment the rest of this suite is talking to."""
    payload = cli("status", "-o", "json").json()

    assert payload["healthy"] is True
    assert payload["ready"] is True


def test_schema_serves_the_write_contract(cli: Any) -> None:
    """This is the field list an assistant writes an astrabox.yaml from. It
    comes from the deployment, so an empty or unversioned answer means the
    caller is guessing."""
    payload = cli("schema", "agent", "-o", "json").json()

    assert payload["version"], "the agent schema carries no version"
    fields = {field["key"]: field for field in payload["fields"]}
    for required in ("name", "model", "environment_name"):
        assert fields[required].get("required") is True, f"{required} is not required"


def test_get_lists_what_the_deployment_holds(cli: Any) -> None:
    environments = cli("get", "environments", "-o", "json").json()

    assert isinstance(environments, list) and environments, "no environments to configure against"
    assert all("name" in row for row in environments)


def test_a_missing_resource_exits_failed_not_zero(cli: Any) -> None:
    """A caller must be able to tell "absent" from "found" by exit code alone."""
    result = cli("get", "agents", f"absent-{uuid.uuid4().hex}", "-o", "json", expect=EXIT_FAILED)

    assert result.json()["ok"] is False


def test_an_unreachable_endpoint_is_its_own_exit_code() -> None:
    """Separated from a refused request because the caller's next action
    differs: fix the address, not the request."""
    env = {**os.environ, "ASTRABOX_ENDPOINT": "http://127.0.0.1:1"}
    env.pop("ASTRABOX_TOKEN", None)
    completed = subprocess.run(
        [sys.executable, "-c", "import sys;from astrabox.cli import main;sys.exit(main(sys.argv[1:]))",
         "status", "-o", "json"],
        capture_output=True, text=True, timeout=60, env=env,
    )

    assert completed.returncode == EXIT_UNREACHABLE
    assert json.loads(completed.stdout)["ok"] is False


def test_export_round_trips_through_diff(cli: Any, tmp_path: Path) -> None:
    """On an unambiguous deployment, what export writes, apply accepts.

    The assertion is that nothing would be *created* — every exported resource
    is recognised as already present. It is deliberately not "everything is
    unchanged": an environment's stored secret comes back masked, so a document
    carrying the mask reports that field as differing on every diff while the
    write stays idempotent.
    """
    document = tmp_path / "astrabox.yaml"
    cli("init", "--from-deployment", "-f", str(document), "-o", "json")

    assert document.is_file(), "init --from-deployment wrote no document"
    rows = cli("diff", "-f", str(document), "-o", "json").json()["resources"]

    assert rows, "the exported document declared nothing"
    created = [row for row in rows if row["action"] == "create"]
    assert not created, f"the export is not round-trippable; apply would create {created}"

    # Agents carry no masked field, so their round trip must be exact. This is
    # the assertion that catches a comparison reading a nested field at the
    # wrong place: `display_name` is stored at `display_meta.display_name`, and
    # a flat lookup reports it as changed on every run forever.
    drifting = [
        row for row in rows if row["kind"] == "agent" and row["action"] != "unchanged"
    ]
    assert not drifting, (
        "an exported Agent does not converge against the deployment it came from: "
        f"{drifting}"
    )


def test_apply_creates_then_destroy_removes(
    cli: Any, e2e_client: httpx.Client, tmp_path: Path
) -> None:
    """The write half, end to end, on a resource this test owns.

    A document declaring one Agent must produce exactly one create, be
    unchanged on a second diff, appear in `get`, and leave nothing behind.
    """
    environment = _agent_environment(cli)
    environment_name = str(environment["name"])
    models = e2e_client.get(
        f"/api/v1/admin/environments/{environment_name}/models"
    ).json()["data"]["models"]
    model = models[0] if models else "claude-opus-5"

    name = f"cli-e2e-{uuid.uuid4().hex[:8]}"
    document = tmp_path / "astrabox.yaml"
    document.write_text(
        "version: 1\n"
        "agents:\n"
        f"  - name: {name}\n"
        f"    model: {model}\n"
        f"    environment_name: {environment_name}\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    try:
        planned = cli("diff", "-f", str(document), "-o", "json").json()["resources"]
        assert [row["action"] for row in planned] == ["create"]

        applied = cli("apply", "-f", str(document), "-o", "json").json()
        assert [row["action"] for row in applied["resources"]] == ["create"]
        assert applied["dry_run"] is False

        again = cli("diff", "-f", str(document), "-o", "json").json()["resources"]
        assert [row["action"] for row in again] == ["unchanged"], (
            f"applying the same document twice does not converge: {again}"
        )

        listed = cli("get", "agents", name, "-o", "json").json()
        assert listed["name"] == name
        assert listed["model"] == model
    finally:
        cli("destroy", "-f", str(document), "--yes", "-o", "json", expect=None)

    survivors = [
        row for row in cli("get", "agents", "-o", "json").json() if row.get("name") == name
    ]
    assert not survivors, f"destroy left the Agent behind: {survivors}"


def test_apply_refuses_an_assistant_only_environment(
    cli: Any, tmp_path: Path
) -> None:
    """An Agent write must fail before storing an unrunnable engine pairing."""
    environment = _assistant_environment(cli)
    name = f"cli-e2e-incompatible-{uuid.uuid4().hex[:8]}"
    document = tmp_path / "astrabox.yaml"
    document.write_text(
        "version: 1\n"
        "agents:\n"
        f"  - name: {name}\n"
        "    model: assistant-model-is-not-used\n"
        f"    environment_name: {environment['name']}\n",
        encoding="utf-8",
    )

    try:
        result = cli("apply", "-f", str(document), "-o", "json", expect=EXIT_FAILED)
        failure = result.json()
        assert failure["ok"] is False
        assert failure["code"] == "INVALID_REQUEST"
        assert "session_kind='agent_chat'" in failure["error"]
    finally:
        # If an older Server accepted the bad write, retain the red result but
        # remove the test-owned record before another run starts.
        cli("destroy", "-f", str(document), "--yes", "-o", "json", expect=None)

    survivors = [
        row for row in cli("get", "agents", "-o", "json").json()
        if row.get("name") == name
    ]
    assert not survivors, f"the rejected Agent was stored: {survivors}"


def test_dry_run_apply_writes_nothing(cli: Any, tmp_path: Path) -> None:
    """--dry-run must be the same reads with none of the writes, or a caller
    cannot use it to preview against a deployment it does not own."""
    name = f"cli-e2e-dry-{uuid.uuid4().hex[:8]}"
    environment = _agent_environment(cli)
    document = tmp_path / "astrabox.yaml"
    document.write_text(
        "version: 1\n"
        "agents:\n"
        f"  - name: {name}\n"
        "    model: claude-opus-5\n"
        f"    environment_name: {environment['name']}\n",
        encoding="utf-8",
    )

    payload = cli("apply", "-f", str(document), "--dry-run", "-o", "json").json()

    assert payload["dry_run"] is True
    assert [row["action"] for row in payload["resources"]] == ["create"]
    survivors = [
        row for row in cli("get", "agents", "-o", "json").json() if row.get("name") == name
    ]
    assert not survivors, "--dry-run created the Agent"


def test_the_mcp_facade_answers_the_same_deployment(cli: Any, e2e_base_url: str) -> None:
    """One implementation, two façades: the MCP tool must reach the deployment
    the argv command reaches, not a second code path with its own defaults."""
    env = {**os.environ, "ASTRABOX_ENDPOINT": e2e_base_url}
    raw = os.getenv("ASTRABOX_E2E_AUTH_TOKEN_FILE", "")
    if raw:
        env["ASTRABOX_TOKEN"] = Path(raw).read_text(encoding="utf-8").strip()
    requests = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "astrabox_get", "arguments": {"kind": "environments"}},
                }
            ),
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import sys;from astrabox.cli import main;sys.exit(main(sys.argv[1:]))",
         "mcp", "serve"],
        input=requests + "\n",
        capture_output=True, text=True, timeout=120, env=env,
    )

    assert completed.returncode == 0, completed.stderr[:500]
    answers = {
        message["id"]: message
        for message in (json.loads(line) for line in completed.stdout.splitlines() if line.strip())
    }
    assert set(answers) == {1, 2, 3}, "a notification drew a response, or one was lost"
    assert answers[2]["result"]["tools"], "tools/list returned nothing"
    listed = answers[3]["result"]
    assert listed["isError"] is False
    assert listed["structuredContent"]["items"], "the MCP tool saw no environments"

def test_run_answers_a_task_end_to_end(
    cli: Any, e2e_client: httpx.Client, tmp_path: Path
) -> None:
    """The whole developer loop in one command: declare an Agent, apply it, and
    give it work.

    This is the case that needs a live deployment rather than a fake: a
    conversation exists before its sandbox does, so a turn sent the moment the
    session id comes back is refused with SESSION_BUSY. Nothing short of a real
    sandbox reproduces that window.
    """
    environment = _agent_environment(cli)
    environment_name = str(environment["name"])
    models = e2e_client.get(
        f"/api/v1/admin/environments/{environment_name}/models"
    ).json()["data"]["models"]
    assert models, f"environment {environment_name} offers no model to run against"

    name = f"cli-e2e-run-{uuid.uuid4().hex[:8]}"
    document = tmp_path / "astrabox.yaml"
    document.write_text(
        "version: 1\n"
        "agents:\n"
        f"  - name: {name}\n"
        f"    model: {models[0]}\n"
        f"    environment_name: {environment_name}\n"
        "    system: Answer in one short sentence. Do not use tools.\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    cli("apply", "-f", str(document), "-o", "json")

    try:
        payload = cli(
            "run", name, "What is 7 times 6? Reply with just the number, nothing else.",
            "--timeout", "150", "-o", "json",
        ).json()

        assert payload["session_id"], "run reported no session"
        assert not payload["errors"], f"the turn carried errors: {payload['errors']}"
        assert payload["pending_interaction"] is None, (
            "the Agent stopped to ask something; the prompt was meant to need no tool"
        )
        assert "42" in payload["text"], f"unexpected answer: {payload['text'][:200]}"
    finally:
        cli("destroy", "-f", str(document), "--yes", "-o", "json", expect=None)
