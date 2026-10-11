"""E2E: a conversation's own box at the default size carries a turn that works.

A conversation's own box is 1 CPU and 1Gi unless the deployment says
otherwise. The engine has to fit in it while it works, not only while it idles:
the turn here writes a file and runs it. The evidence is the box's own cgroup,
read from inside the box after the turn: its memory ceiling is the default, and
the kernel recorded no OOM kill in it.

Run on a deployment that leaves ASTRABOX_SANDBOX_MEMORY_LIMIT unset:

    .venv/bin/python -m pytest tests/e2e/test_own_box_size.py -m e2e -s
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    create_agent_variant_session,
    environment_with_tenancy,
    file_names,
    permission_mode,
    poll_until_agent_ready,
    run_terminal,
    stream_turn,
)

pytestmark = pytest.mark.e2e

_DEFAULT_MEMORY_BYTES = 1024**3
#: sum(i * i for i in range(1, 2001))
_EXPECTED_SUM = "2668667000"
_PROMPT = (
    "In the current directory, create a file named squares.py containing exactly "
    "`print(sum(i * i for i in range(1, 2001)))`, run it with python3, and reply "
    "with the number it printed and nothing else."
)


def _cgroup_file(client: httpx.Client, sid: str, name: str) -> str:
    stdout, stderr, exit_code, _ = run_terminal(client, sid, f"cat /sys/fs/cgroup/{name}")
    assert exit_code == 0, f"reading the box's {name} failed: exit={exit_code} stderr={stderr!r}"
    return stdout


def test_a_conversation_own_box_at_the_default_size_finishes_a_working_turn(
    e2e_client: httpx.Client,
) -> None:
    detail = create_agent_variant_session(
        e2e_client,
        name_suffix="own box size",
        engine_options={},
        permission_mode=permission_mode("unattended"),
        environment_name=environment_with_tenancy(e2e_client, "conversation"),
    )
    sid = str(detail.get("session_id") or "")
    poll_until_agent_ready(e2e_client, sid)

    ceiling = _cgroup_file(e2e_client, sid, "memory.max").strip()
    assert ceiling == str(_DEFAULT_MEMORY_BYTES), (
        f"the conversation's own box is not at the default 1Gi: memory.max={ceiling!r}"
    )

    result = stream_turn(e2e_client, sid, content=_PROMPT)
    assert result.error is None, f"the turn errored: {result.error}"
    assert result.finish_reason == "stop", f"the turn did not finish: {result.finish_reason!r}"
    assert _EXPECTED_SUM in result.text.replace(",", ""), (
        f"the reply lacks what the script prints: {result.text!r}"
    )
    assert "squares.py" in file_names(e2e_client, sid), "the turn did not write the file"

    events = dict(
        line.split()
        for line in _cgroup_file(e2e_client, sid, "memory.events").splitlines()
        if line.strip()
    )
    assert int(events.get("oom_kill", "0")) == 0, (
        f"the kernel killed a process in the box for memory during the turn: {events}"
    )
