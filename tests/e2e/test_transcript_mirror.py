"""E2E proof that the in-box transcript mirror lands on the host store and comes back.

This is the load-bearing networking + producer proof for restoring a session
into a replacement box. Every engine's box POSTs its transcript lines to the
host ``/api/v1/transcript/{session_id}/append`` endpoint (the URL is built from
``ASTRABOX_MCP_PROXY_BASE_URL``); the host persists each line into the durable
``transcript_entries`` store — the externalized runtime truth recovery reads
instead of the (possibly-dead) sandbox.

A turn that streams text but leaves ``transcript_entries`` empty is the failure
signature this test exists to catch: the container could not reach the host (a
mis-wired / unreachable ``ASTRABOX_MCP_PROXY_BASE_URL`` — e.g. pointing at
``host.docker.internal`` on a box where that is intercepted, instead of the host's
LAN IP). That is a hard ``assert`` here, never a silent pass.

What it proves, end to end:

* a first real turn plants a marker, and its ``transcript_entries`` rows land in
  the running stack's PostgreSQL before the box is killed; and
* a replacement box answers with the marker, which only the restored transcript
  holds.

Run it against the WIRED running stack (reuses it; boots nothing):

    ASTRABOX_E2E_BASE_URL=http://localhost:8000 \
      .venv/bin/python -m pytest tests/e2e/test_transcript_mirror.py -m e2e -s

The SQL collection adapter stores every logical "collection" as rows in a single
``astrabox_documents`` table partitioned by the ``collection`` column, with the
document in the JSONB ``doc`` column; so ``transcript_entries`` rows are
``collection='transcript_entries'`` and the mirror fields (``platform_session_id``,
``entry_json`` …) live inside ``doc`` (see
the shared SQL document adapter).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    create_agent_variant_session,
    environment_with_tenancy,
    engine_kind,
    data,
    get_admin_session_detail,
    get_session,
    release_session,
    poll_until_agent_ready,
    stream_turn,
    wait_until_settled,
)
from tests.e2e._service_containers import (
    POSTGRES_CONTAINER_HANDLE,
    require_service_container,
)

pytestmark = pytest.mark.e2e

# The mirror flush (batcher -> HTTP POST -> host persist) is async + best-effort,
# so poll for it to land rather than reading once.
MIRROR_LAND_TIMEOUT_S = float(os.getenv("ASTRABOX_E2E_MIRROR_TIMEOUT", "90"))


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _postgres_container() -> str:
    return require_service_container(POSTGRES_CONTAINER_HANDLE)


def _psql(sql: str) -> list[str]:
    database = os.getenv("ASTRABOX_E2E_POSTGRES_DB", "astrabox")
    user = os.getenv("ASTRABOX_E2E_POSTGRES_USER", "astrabox")
    output = subprocess.check_output(
        [
            "docker",
            "exec",
            _postgres_container(),
            "psql",
            "-X",
            "-A",
            "-t",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            user,
            "-d",
            database,
            "-c",
            sql,
        ],
        text=True,
        timeout=20,
    )
    return [line for line in output.splitlines() if line]


def _assert_pi_settings(client: httpx.Client, sid: str, expected: dict) -> None:
    """Read native config from the actual original or restored conversation."""

    from tests.e2e.test_terminal import _run_terminal

    identity = get_admin_session_detail(client, sid).get("runtime_identity") or {}
    home = str(identity.get("home_dir") or "")
    assert home, identity
    stdout, stderr, code, events = _run_terminal(
        client, sid, "cat " + shlex.quote(f"{home}/.pi/agent/settings.json")
    )
    assert code == 0, (stderr, events)
    actual = json.loads(stdout)
    assert {key: actual[key] for key in expected} == expected


def _query_mirror_docs(platform_session_id: str) -> list[dict]:
    """Read ``transcript_entries`` docs for one platform session, in append order.

    Reads the live backend's committed PostgreSQL rows directly.
    """
    rows = _psql(
        "SELECT doc::text FROM astrabox_documents "
        "WHERE collection='transcript_entries' "
        f"AND doc ->> 'platform_session_id' = {_sql_literal(platform_session_id)} "
        "ORDER BY doc -> 'seq', seq"
    )
    return [json.loads(row) for row in rows]


def _wait_for_mirror_docs(
    platform_session_id: str, timeout: float, *, until: tuple[str, ...] = ()
) -> list[dict]:
    """Wait for the rows the caller is about to assert on, not for any row.

    "Any row" is not the same question for every engine: one opens its log when
    the conversation is created and has already recorded the permission preset
    the platform set, so a wait for a non-empty store returns instantly with a
    set that predates the turn — and the assertion below then fails on a mirror
    that was merely still catching up.
    """

    deadline = time.monotonic() + timeout
    docs: list[dict] = []
    while time.monotonic() < deadline:
        docs = _query_mirror_docs(platform_session_id)
        if docs:
            present = {json.loads(d["entry_json"]).get("type") for d in docs}
            if all(name in present for name in until):
                return docs
        time.sleep(1.5)
    return docs


def _assert_db_is_the_live_stack(sid: str, timeout: float = 15.0) -> None:
    """Prove the selected PostgreSQL container belongs to the live deployment."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _psql(
            "SELECT 1 FROM astrabox_documents "
            f"WHERE doc ->> 'session_id' = {_sql_literal(sid)} LIMIT 1"
        )
        if rows:
            return
        time.sleep(1.0)
    pytest.fail(
        f"session {sid} is not in PostgreSQL container {_postgres_container()} — "
        "set ASTRABOX_E2E_POSTGRES_CONTAINER to the database used by the server."
    )


def _terminate_sandbox(client: httpx.Client, sid: str) -> str:
    """Reclaim the session's sandbox and return the id it reports killing.

    A turn's stream can close a beat before `conversation_state` leaves
    STREAMING, so a terminate right after a settled turn can race a still-active
    turn — a 409 the server marks retryable.
    """
    deadline = time.monotonic() + 30.0
    while True:
        resp = client.post(f"/api/v1/sessions/{sid}/sandbox/terminate", timeout=60.0)
        if resp.status_code == 409 and time.monotonic() < deadline:
            time.sleep(1.0)
            continue
        return str(data(resp).get("sandbox_id") or "")


@pytest.mark.xdist_group("conversation-tenancy-box")
def test_a_replacement_box_answers_from_the_restored_transcript(
    e2e_client: httpx.Client,
) -> None:
    """The lines leave the box, come BACK, and only they can answer.

    A first turn plants a marker, that sandbox is terminated, recovery provisions
    a new one, and the next turn is asked what the marker was. The box
    that heard it does not exist any more, so a correct answer can only come from
    the transcript the platform put back.

    Waiting for the store before the kill is part of the contract, not a
    workaround: the relay is a resident program on a poll, so "the box streamed a
    turn" and "the store holds that turn" are different moments. Killing between
    them exercises the dropped-resume-key path instead, which is a different
    behaviour with its own gates.

    It needs **conversation** tenancy for the same reason it needs the kill: the
    proof rests on the box that heard the marker being gone. Under agent tenancy
    terminate closes this conversation's isolated session and leaves the Agent's
    box running, so the engine could answer from the box and the test would pass
    without the mirror doing anything.
    """

    marker = "MEM" + uuid.uuid4().hex[:6].upper()
    pi_settings = {
        "defaultThinkingLevel": "off",
        "compaction": {"enabled": False, "keepRecentTokens": 12000},
        "futureVendorSetting": {"marker": marker},
    }
    created = create_agent_variant_session(
        e2e_client,
        name_suffix="mirror-restore",
        engine_options={"settings": pi_settings} if engine_kind() == "pi" else {},
        environment_name=environment_with_tenancy(e2e_client, "conversation"),
    )
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)
        first_box = str(get_session(e2e_client, sid).get("sandbox_id") or "")
        assert first_box, "a READY session should hold a sandbox_id"
        if engine_kind() == "pi":
            _assert_pi_settings(e2e_client, sid, pi_settings)

        res = stream_turn(
            e2e_client,
            sid,
            content=(
                f"Our hypothetical investment research project is labelled {marker}. "
                "For this project, explain in one sentence what revenue growth measures. "
                "Include the project label in your answer. This is a general definition, "
                "so no external research or workspace artifact is needed."
            ),
        )
        assert res.error is None, f"first turn errored: {res.error}"
        assert res.n_text_delta > 0, "first turn streamed no text-delta"
        assert marker.upper() in res.text.upper(), (
            f"first turn did not establish the project label: {res.text!r}"
        )
        wait_until_settled(e2e_client, sid)

        docs = _wait_for_mirror_docs(sid, timeout=MIRROR_LAND_TIMEOUT_S)
        assert docs, (
            f"nothing mirrored for session {sid} before the kill; the restore "
            "path cannot be under test"
        )

        killed = _terminate_sandbox(e2e_client, sid)
        assert killed == first_box, f"terminate reclaimed {killed!r}, not {first_box!r}"

        data(e2e_client.post(f"/api/v1/sessions/{sid}/recover", timeout=60.0))
        poll_until_agent_ready(e2e_client, sid)
        second_box = str(get_session(e2e_client, sid).get("sandbox_id") or "")
        assert second_box and second_box != first_box, (
            f"recovery reused {second_box!r}; the restore must run in a NEW box"
        )
        if engine_kind() == "pi":
            _assert_pi_settings(e2e_client, sid, pi_settings)

        recalled = stream_turn(
            e2e_client,
            sid,
            content=(
                "What was the project label I gave you for our revenue-growth discussion? "
                "Reply with that original label."
            ),
        )
        assert recalled.error is None, f"second turn errored: {recalled.error}"
        assert marker.upper() in recalled.text.upper(), (
            f"the replacement box did not recall {marker!r} from the restored "
            f"transcript; it answered: {recalled.text!r}"
        )
    finally:
        release_session(sid)
