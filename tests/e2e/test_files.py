"""Files e2e — end-to-end coverage for the basic file capability.

This proves that the session sandbox exposes OpenSandbox's native Filesystem API
and that the host's ``session_file_service`` uses it for the workspace panel.

What it proves (mirrors the wiring of ``test_live_turn.py``):

* starting an agent conversation provisions a sandbox that reaches READY.
* ``POST /api/v1/sessions/{id}/files/list`` returns a valid directory listing
  (a ``root_path`` + an ``entries`` list) through the SDK Filesystem adapter.
* an upload → list → download round-trip preserves the bytes.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_files.py -m e2e -s
"""

from __future__ import annotations

import uuid
import shlex

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    create_session,
    data,
    list_files,
    poll_until_agent_ready,
    release_session,
    run_terminal,
)

pytestmark = pytest.mark.e2e


def test_files_list_returns_listing(e2e_client: httpx.Client) -> None:
    """create -> READY -> files/list returns a valid listing."""
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        listing = list_files(e2e_client, sid)
        # The load-bearing assertions: the native Filesystem API returned a
        # well-formed listing.
        assert isinstance(listing.get("entries"), list), f"entries not a list: {listing}"
        assert str(listing.get("root_path") or "").startswith("/"), (
            f"missing/invalid root_path: {listing}"
        )
        root = shlex.quote(listing["root_path"])
        out, err, rc, events = run_terminal(
            e2e_client, sid,
            f"find {root} -maxdepth 1 "
            "\\( -name '.astrabox-*' -o -name 'Downloads' \\) -print",
        )
        assert rc == 0, f"Could not inspect the actual Workspace: {err}, {events}"
        assert not out.strip(), f"Platform artifacts polluted the user's Workspace: {out}"
        for entry in listing["entries"]:
            assert entry.get("name"), f"listing entry missing name: {entry}"
            assert str(entry.get("path") or "").startswith("/"), f"bad entry path: {entry}"
            assert entry.get("kind") in {"file", "directory"}, f"bad entry kind: {entry}"
    finally:
        release_session(sid)


def test_files_upload_list_download_round_trip(e2e_client: httpx.Client) -> None:
    """create -> READY -> upload -> the file lists -> download returns the same bytes."""
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    filename = f"astrabox_e2e_{uuid.uuid4().hex[:10]}.txt"
    content = f"hello astrabox files e2e {uuid.uuid4()}\n".encode()
    try:
        poll_until_agent_ready(e2e_client, sid)

        # ── upload into the session root (path="") ───────────────────────────
        uploaded = data(
            e2e_client.post(
                f"/api/v1/sessions/{sid}/files/upload",
                data={"path": ""},
                files={"files": (filename, content, "text/plain")},
            )
        )
        assert int(uploaded.get("uploaded_count") or 0) == 1, f"upload failed: {uploaded}"
        entries = uploaded.get("entries") or []
        assert entries and entries[0].get("name") == filename, f"unexpected upload result: {uploaded}"
        dest_path = str(entries[0].get("path") or "")
        assert dest_path.startswith("/"), f"upload returned no absolute path: {uploaded}"

        # ── the uploaded file appears in the listing ─────────────────────────
        listing = list_files(e2e_client, sid)
        names = {str(e.get("name")) for e in listing.get("entries") or []}
        assert filename in names, f"uploaded {filename!r} not in listing names={names}"

        # ── download returns byte-identical content ──────────────────────────
        dl = e2e_client.get(
            f"/api/v1/sessions/{sid}/files/download", params={"path": dest_path}
        )
        assert dl.status_code == 200, f"download -> {dl.status_code}: {dl.text[:300]}"
        assert dl.content == content, (
            f"download mismatch: got {dl.content!r} expected {content!r}"
        )
    finally:
        release_session(sid)
