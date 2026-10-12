"""Files e2e — end-to-end coverage for the basic file capability.

This proves that the session sandbox exposes OpenSandbox's native Filesystem API
and that the host's ``session_file_service`` uses it for the workspace panel.

What it proves:

* starting an agent conversation provisions a sandbox that reaches READY.
* ``POST /api/v1/sessions/{id}/files/list`` returns a valid directory listing
  (a ``root_path`` + an ``entries`` list) through the SDK Filesystem adapter.
* an upload → list → download round-trip preserves the bytes.
* a file far larger than the server's memory budget (10 GiB unless
  ``ASTRABOX_E2E_LARGE_FILE_BYTES`` says otherwise) uploads, and downloads
  through the owner's route and a share link, byte for byte, while the API
  process's resident memory stays within a bound that does not grow with the
  file; a client that leaves mid-download is released and the server keeps
  serving.
* an empty file, a non-ASCII name, a directory, a missing file and a path
  outside the root are each answered before a download's body starts.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_files.py -m e2e -s
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import random
import shlex
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    create_session,
    data,
    download,
    list_files,
    poll_until_agent_ready,
    release_session,
    run_terminal,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container

pytestmark = pytest.mark.e2e

#: The large file's size. The default is the size the platform has to carry.
LARGE_FILE_BYTES = int(os.getenv("ASTRABOX_E2E_LARGE_FILE_BYTES", str(10 * 1024**3)))

#: How far the API process's resident memory may rise while it carries the
#: file. The bound is fixed and the file is at least twice its size, so a
#: transfer holding the file, or any share of it that grows with the file,
#: crosses it.
RSS_GROWTH_BOUND_BYTES = 512 * 1024 * 1024

_BLOCK_BYTES = 1024 * 1024

#: Run inside the server container: find the one ``astrabox serve`` process,
#: optionally reset its peak resident set to its current one (proc(5),
#: ``/proc/<pid>/clear_refs`` value 5), and print its pid, current RSS and peak
#: RSS in bytes. The peak is the kernel's own high-water mark, so a short spike
#: between two reads is still seen.
_API_MEMORY_PROBE = r"""
import os, sys
found = []
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    try:
        with open(f"/proc/{entry}/cmdline", "rb") as handle:
            argv = [part for part in handle.read().split(b"\0") if part]
    except OSError:
        continue
    if len(argv) == 3 and argv[2] == b"serve" and os.path.basename(argv[1]) == b"astrabox":
        found.append(entry)
if len(found) != 1:
    sys.exit(f"expected one 'astrabox serve' process, found pids {found}")
pid = found[0]
if sys.argv[1] == "reset":
    with open(f"/proc/{pid}/clear_refs", "w") as handle:
        handle.write("5")
fields = {}
with open(f"/proc/{pid}/status") as handle:
    for line in handle:
        key, _, value = line.partition(":")
        fields[key] = value.split()
print(pid, int(fields["VmRSS"][0]) * 1024, int(fields["VmHWM"][0]) * 1024)
"""


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


# ── large files ──────────────────────────────────────────────────────────────


def _api_memory(server: str, *, reset: bool) -> tuple[int, int]:
    """The API process's current and peak resident memory, in bytes."""

    result = subprocess.run(
        ["docker", "exec", server, "python", "-c", _API_MEMORY_PROBE, "reset" if reset else "read"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"could not read the API process's memory in {server}: "
        f"{(result.stderr or result.stdout).strip()[-400:]}"
    )
    _pid, rss, peak = result.stdout.split()
    return int(rss), int(peak)


def _content(size: int, seed: str) -> Iterator[bytes]:
    """The test file's bytes, made as they are sent: one block in memory at a time.

    Each block starts with its own index, so a block lost, repeated or moved on
    the way changes the digest. The rest is pseudo-random, so nothing on the
    path can carry the file in less space by compressing it.
    """

    base = random.Random(seed).randbytes(_BLOCK_BYTES)
    for index, offset in enumerate(range(0, size, _BLOCK_BYTES)):
        block = index.to_bytes(8, "big") + base[8:]
        yield block[: size - offset]


def _transfer_progress(log: Path, stage: str, **facts: object) -> None:
    with log.open("a") as stream:
        stream.write(json.dumps({"time": time.time(), "stage": stage, **facts}) + "\n")


def _upload_streamed(
    client: httpx.Client, sid: str, *, name: str, size: int, seed: str, deadline: float,
    progress: Path,
) -> tuple[dict, str]:
    """Upload a generated file through the Files API; return the answer and the SHA-256 sent."""

    digest = hashlib.sha256()
    boundary = f"astrabox-e2e-{uuid.uuid4().hex}"
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="path"\r\n\r\n\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def body() -> Iterator[bytes]:
        yield head
        sent = 0
        for block in _content(size, seed):
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"the upload had sent {sent} of {size} bytes when the live test budget ran out"
                )
            digest.update(block)
            sent += len(block)
            yield block
            if sent // 1024**3 != (sent - len(block)) // 1024**3:
                _transfer_progress(progress, "upload-sent", bytes=sent)
        yield tail
        _transfer_progress(progress, "upload-body-sent", bytes=sent)

    response = client.post(
        f"/api/v1/sessions/{sid}/files/upload",
        content=body(),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(head) + size + len(tail)),
        },
        # The answer comes once the server has written the file into the box,
        # which takes as long again as sending it; only the budget bounds it.
        timeout=httpx.Timeout(60.0, read=max(1.0, deadline - time.monotonic())),
    )
    _transfer_progress(progress, "upload-response", status=response.status_code)
    return data(response), digest.hexdigest()


def _download_digest(
    base_url: str, headers: dict[str, str], url: str, path: str, *, deadline: float,
    progress: Path, route: str,
) -> tuple[int, str, httpx.Headers]:
    """Stream one download into SHA-256; return the bytes received, the digest and the headers."""

    digest = hashlib.sha256()
    received = 0
    with httpx.Client(base_url=base_url, headers=headers) as client:
        with client.stream(
            "GET", url, params={"path": path}, timeout=httpx.Timeout(30.0, read=60.0)
        ) as response:
            assert response.status_code == 200, (
                f"GET {url} -> {response.status_code}: {response.read()[:400]!r}"
            )
            for chunk in response.iter_raw(_BLOCK_BYTES):
                if time.monotonic() > deadline:
                    raise AssertionError(
                        f"GET {url} had received {received} bytes when the live test budget ran out"
                    )
                digest.update(chunk)
                received += len(chunk)
                if received // 1024**3 != (received - len(chunk)) // 1024**3:
                    _transfer_progress(progress, "download-received", route=route, bytes=received)
            _transfer_progress(progress, "download-complete", route=route, bytes=received)
            return received, digest.hexdigest(), response.headers


def _server_logged(server: str, *, since: float, needle: str, deadline: float) -> bool:
    while True:
        logs = subprocess.run(
            ["docker", "logs", "--since", str(int(since)), server],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if needle in logs.stdout or needle in logs.stderr:
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)


def test_a_file_larger_than_the_servers_memory_budget_round_trips(
    e2e_client: httpx.Client,
    e2e_base_url: str,
    e2e_auth_headers: dict[str, str],
    live_test_deadline: float,
    tmp_path: Path,
) -> None:
    """upload -> list -> abandoned download -> owner and share downloads, byte for byte.

    The file is generated while it is sent and hashed while it is received, so
    the test process never holds it either. A download that buffered the file,
    or refused it for its size, fails here; so does an upload that waited for
    the box with a timeout that does not grow with the file.
    """

    size = LARGE_FILE_BYTES
    assert size >= 2 * RSS_GROWTH_BOUND_BYTES, (
        f"ASTRABOX_E2E_LARGE_FILE_BYTES={size} is too small: below "
        f"{2 * RSS_GROWTH_BOUND_BYTES} bytes the memory bound cannot tell a streamed "
        "transfer from a buffered one"
    )
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    name = f"astrabox_e2e_large_{uuid.uuid4().hex[:10]}.bin"
    progress = tmp_path / "large-file-transfer.jsonl"
    _transfer_progress(progress, "session-created", session_id=sid, filename=name, bytes=size)
    try:
        poll_until_agent_ready(e2e_client, sid)
        _transfer_progress(progress, "session-ready")

        # ── upload ───────────────────────────────────────────────────────────
        before, _ = _api_memory(server, reset=True)
        started = time.monotonic()
        uploaded, sent_digest = _upload_streamed(
            e2e_client, sid, name=name, size=size, seed=name, deadline=live_test_deadline,
            progress=progress,
        )
        upload_seconds = time.monotonic() - started
        _transfer_progress(progress, "upload-complete", seconds=upload_seconds)
        _, peak = _api_memory(server, reset=False)
        assert peak - before < RSS_GROWTH_BOUND_BYTES, (
            f"the API process grew by {peak - before} bytes while it took a {size}-byte upload "
            f"({upload_seconds:.0f}s); the bound is {RSS_GROWTH_BOUND_BYTES}"
        )
        entries = uploaded.get("entries") or []
        assert int(uploaded.get("uploaded_count") or 0) == 1 and entries, uploaded
        dest_path = str(entries[0].get("path") or "")
        listed = {str(e.get("name")): e for e in list_files(e2e_client, sid).get("entries") or []}
        assert int((listed.get(name) or {}).get("size") or -1) == size, (
            f"the box does not hold all {size} bytes of {name}: {listed.get(name)}"
        )

        share = data(e2e_client.post(f"/api/v1/sessions/{sid}/share", json={"allow_download": True}))
        token = str(share.get("token") or "")
        assert token, f"share link was not created: {share}"
        owner_url = f"/api/v1/sessions/{sid}/files/download"
        share_url = f"/api/v1/share/{token}/files/download"

        # ── a client that leaves after the first few blocks ──────────────────
        # Its trace id comes back in the server's own note that the download
        # ended early, which is logged only after the box connection the
        # download held has been released.
        trace_id = uuid.uuid4().hex
        left_at = time.time() - 5
        with httpx.Client(
            base_url=e2e_base_url,
            headers={**e2e_auth_headers, "traceparent": f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01"},
        ) as client:
            with client.stream(
                "GET", owner_url, params={"path": dest_path}, timeout=httpx.Timeout(30.0)
            ) as response:
                assert response.status_code == 200, response.read()[:400]
                taken = 0
                for chunk in response.iter_raw(_BLOCK_BYTES):
                    taken += len(chunk)
                    if taken >= 8 * _BLOCK_BYTES:
                        break
        assert _server_logged(
            server,
            since=left_at,
            needle=f"trace_id={trace_id}",
            deadline=min(time.monotonic() + 20, live_test_deadline),
        ), "the server never recorded the abandoned download as ended, so its box connection may still be held"
        assert e2e_client.get("/healthz").status_code == 200

        # ── the owner's download and the share link's, side by side ───────────
        before, _ = _api_memory(server, reset=True)
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(
                _download_digest, e2e_base_url, e2e_auth_headers, owner_url, dest_path,
                deadline=live_test_deadline,
                progress=progress, route="owner",
            )
            shared = pool.submit(
                _download_digest, e2e_base_url, e2e_auth_headers, share_url, dest_path,
                deadline=live_test_deadline,
                progress=progress, route="share",
            )
            results = {"owner": owner.result(), "share": shared.result()}
        download_seconds = time.monotonic() - started
        _, peak = _api_memory(server, reset=False)
        for route, (received, digest, headers) in results.items():
            assert received == size, f"{route} download delivered {received} of {size} bytes"
            assert digest == sent_digest, f"{route} download's SHA-256 differs from the upload's"
            assert headers.get("content-length") == str(size), (route, headers)
            assert name in headers.get("content-disposition", ""), (route, headers)
        assert peak - before < RSS_GROWTH_BOUND_BYTES, (
            f"the API process grew by {peak - before} bytes while it served two {size}-byte "
            f"downloads ({download_seconds:.0f}s); the bound is {RSS_GROWTH_BOUND_BYTES}"
        )
    finally:
        release_session(sid)


def test_files_download_refusals_and_edge_names_answer_before_the_body(
    e2e_client: httpx.Client,
) -> None:
    """Empty file, non-ASCII name, directory, missing file and escape attempts.

    The download decides each of these before its response starts, so a
    refusal is an error envelope with its status, never a 200 with an empty or
    cut body; the empty file and the non-ASCII name are ordinary downloads.
    """

    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    suffix = uuid.uuid4().hex[:8]
    empty_name = f"empty-{suffix}.txt"
    unicode_name = f"报告 ü-{suffix}.txt"
    unicode_body = "第一行\nzweite Zeile\n".encode()
    try:
        poll_until_agent_ready(e2e_client, sid)
        uploaded = data(
            e2e_client.post(
                f"/api/v1/sessions/{sid}/files/upload",
                data={"path": ""},
                files=[
                    ("files", (empty_name, b"", "text/plain")),
                    ("files", (unicode_name, unicode_body, "text/plain")),
                ],
            )
        )
        paths = {str(e.get("name")): str(e.get("path")) for e in uploaded.get("entries") or []}
        assert set(paths) == {empty_name, unicode_name}, uploaded

        empty = download(e2e_client, sid, paths[empty_name])
        assert empty.status_code == 200, empty.text[:400]
        assert (empty.headers.get("content-length"), empty.content) == ("0", b"")

        named = download(e2e_client, sid, paths[unicode_name])
        assert named.status_code == 200, named.text[:400]
        assert named.content == unicode_body
        disposition = named.headers.get("content-disposition", "")
        assert f"filename*=UTF-8''{quote(unicode_name, safe='')}" in disposition, disposition

        root = str(list_files(e2e_client, sid).get("root_path") or "")
        refusals = {
            "a directory": (root, 400, "INVALID_REQUEST"),
            "a missing file": (f"{root}/absent-{suffix}.txt", 404, "FILE_NOT_FOUND"),
            "a relative escape": ("../../../etc/passwd", 400, "INVALID_REQUEST"),
            "an absolute escape": ("/etc/passwd", 400, "INVALID_REQUEST"),
        }
        for case, (path, status, code) in refusals.items():
            refused = download(e2e_client, sid, path)
            assert refused.status_code == status, f"{case}: {refused.status_code} {refused.text[:400]}"
            assert refused.json().get("code") == code, f"{case}: {refused.text[:400]}"
    finally:
        release_session(sid)
