"""The in-box mirror's contract with the rollout file and with the store.

Codex writes its canonical thread record to a rollout JSONL and tells nobody.
The mirror relays those bytes to the platform, and the three things worth
pinning are the ones the protocol does not enforce: that a record still being
written is never sent as a whole one, that a mirror which dies mid-request
re-sends the same bytes under the same identity rather than opening a gap or a
duplicate, and that a box which cannot reach the store refuses to run instead
of running silently unmirrored.
"""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
from typing import Any

import pytest

#: Any engine's root; the seam takes it as an argument rather than knowing one.
_ROOT = "/home/agent/.codex/sessions"

_PROGRAM = (
    Path(__file__).resolve().parents[1]
    / "astrabox/core/service/orchestrator/runtime/astrabox-transcript-mirror"
)


def _load(monkeypatch: pytest.MonkeyPatch, codex_home: Path, state_dir: Path) -> Any:
    """Import the program the way the box runs it: configured by environment."""
    monkeypatch.setenv("_ASTRABOX_TRANSCRIPT_BACKEND_BASE_URL", "http://platform/cap/tok")
    monkeypatch.setenv("_ASTRABOX_PLATFORM_SESSION_ID", "sess-1")
    monkeypatch.setenv("_ASTRABOX_TRANSCRIPT_PROJECT_KEY", "/workspace")
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_MIRROR_ROOT", str(codex_home / "sessions"))
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_MIRROR_GLOB", "*.jsonl")
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_MIRROR_NAMESPACE", "codex/")
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_MIRROR_STATE_DIR", str(state_dir))
    # The program ships without a `.py` suffix, the way the image runs it, so
    # the loader is named rather than inferred from the extension.
    spec = importlib.util.spec_from_loader(
        "_transcript_mirror", SourceFileLoader("_transcript_mirror", str(_PROGRAM))
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingSender:
    """Accepts every batch and remembers it."""

    def __init__(self) -> None:
        self.batches: list[tuple[dict, list, str]] = []

    def append(self, key: dict, entries: list, append_id: str) -> None:
        self.batches.append((key, entries, append_id))


class _FailingSender(_RecordingSender):
    """Records the attempt, then fails the way a store outage does."""

    def append(self, key: dict, entries: list, append_id: str) -> None:
        self.batches.append((key, entries, append_id))
        raise OSError("store unreachable")


def _rollout(codex_home: Path, name: str = "2026/08/18/rollout-x-tid.jsonl") -> Path:
    path = codex_home / "sessions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _append_lines(path: Path, *objects: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for obj in objects:
            handle.write(json.dumps(obj) + "\n")


def _mirror_for(module: Any, path: Path, codex_home: Path) -> Any:
    subpath = module.SUBPATH_PREFIX + str(path.relative_to(codex_home / "sessions"))
    return module.FileMirror(path, subpath, module.state_path_for(subpath))


def test_a_record_still_being_written_is_not_sent_as_a_whole_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch stops at the last newline; a torn tail waits for its rest."""

    home, state = tmp_path / "codex", tmp_path / "state"
    module = _load(monkeypatch, home, state)
    state.mkdir(parents=True, exist_ok=True)
    path = _rollout(home)
    _append_lines(path, {"type": "session_meta"}, {"type": "response_item"})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "event_msg", "payl')  # mid-write

    sender = _RecordingSender()
    mirror = _mirror_for(module, path, home)
    assert mirror.pump(sender) == 2
    assert [e["type"] for e in sender.batches[0][1]] == ["session_meta", "response_item"]

    # The partial line is neither sent nor skipped: completing it delivers it.
    assert mirror.pump(sender) == 0
    with path.open("a", encoding="utf-8") as handle:
        handle.write('oad": {}}\n')
    assert mirror.pump(sender) == 1
    assert sender.batches[1][1] == [{"type": "event_msg", "payload": {}}]


def test_a_mirror_that_died_mid_request_re_sends_the_same_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store decides whether the first attempt landed — by append_id.

    The in-flight range is recorded before the request, so the replacement
    process sends the same bytes under the same id. A new id would append the
    same lines twice; a lost record would leave a hole in the rollout that a
    restored box resumes into.
    """

    home, state = tmp_path / "codex", tmp_path / "state"
    module = _load(monkeypatch, home, state)
    state.mkdir(parents=True, exist_ok=True)
    path = _rollout(home)
    _append_lines(path, {"n": 1}, {"n": 2})

    died = _FailingSender()
    mirror = _mirror_for(module, path, home)
    with pytest.raises(OSError):
        mirror.pump(died)
    assert mirror.offset == 0, "a failed append must not advance the offset"

    # A fresh process over the same state directory, as supervisord would give.
    revived = _mirror_for(module, path, home)
    sender = _RecordingSender()
    assert revived.pump(sender) == 2
    assert sender.batches[0][2] == died.batches[0][2], "the batch identity changed"
    assert sender.batches[0][1] == died.batches[0][1]
    assert revived.offset == path.stat().st_size


def test_a_subagent_rollout_is_mirrored_under_a_path_that_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box serves one conversation, so every rollout in it belongs to it.

    Codex resolves a thread by the uuid in its filename, and a subagent thread
    writes its own file. The scope carries the path relative to `sessions/`, so
    restoring a box is writing each scope back where Codex will find it.
    """

    home, state = tmp_path / "codex", tmp_path / "state"
    module = _load(monkeypatch, home, state)
    state.mkdir(parents=True, exist_ok=True)
    main = _rollout(home, "2026/08/18/rollout-a-0000.jsonl")
    sub = _rollout(home, "2026/08/18/rollout-b-1111.jsonl")
    _append_lines(main, {"n": 1})
    _append_lines(sub, {"n": 2})

    mirrors: dict[str, Any] = {}
    module.discover(mirrors)
    assert sorted(mirrors) == [
        "codex/2026/08/18/rollout-a-0000.jsonl",
        "codex/2026/08/18/rollout-b-1111.jsonl",
    ]
    for subpath, mirror in mirrors.items():
        assert (
            home / "sessions" / subpath[len(module.SUBPATH_PREFIX):]
        ) == mirror.path


def test_a_rollout_that_disappears_is_reported_once_not_gone_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex does not delete a rollout it is writing; compression renames it.

    Whatever the file holds past the mirrored offset stops being reachable as
    text at that moment, and a mirror that simply found nothing to send would
    be indistinguishable from one that was up to date.
    """

    home, state = tmp_path / "codex", tmp_path / "state"
    module = _load(monkeypatch, home, state)
    state.mkdir(parents=True, exist_ok=True)
    path = _rollout(home)
    _append_lines(path, {"n": 1})
    sender = _RecordingSender()
    mirror = _mirror_for(module, path, home)
    assert mirror.pump(sender) == 1

    path.rename(path.with_suffix(".jsonl.zst"))
    capsys.readouterr()
    assert mirror.pump(sender) == 0
    first = capsys.readouterr().out
    assert "STOPPED" in first and "disappeared" in first
    # Once, not once per poll: a line every half second is noise, not a signal.
    assert mirror.pump(sender) == 0
    assert capsys.readouterr().out == ""


def test_the_scope_names_the_session_and_the_rollout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, state = tmp_path / "codex", tmp_path / "state"
    module = _load(monkeypatch, home, state)
    state.mkdir(parents=True, exist_ok=True)
    path = _rollout(home)
    _append_lines(path, {"n": 1})
    sender = _RecordingSender()
    _mirror_for(module, path, home).pump(sender)
    key = sender.batches[0][0]
    assert key["session_id"] == "sess-1"
    assert key["project_key"] == "/workspace"
    assert key["subpath"] == "codex/2026/08/18/rollout-x-tid.jsonl"


def test_a_box_that_cannot_reach_the_store_refuses_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unmirrored is not a degraded mode: the loss is silent until a resume."""

    module = _load(monkeypatch, tmp_path / "codex", tmp_path / "state")
    monkeypatch.setattr(module, "BASE_URL", "")
    assert module.main() == 78  # EX_CONFIG


# ── the other side of the box: what the platform puts on the box-create env ──


def _manager(base_url: str) -> Any:
    from types import SimpleNamespace

    # Match EnginePlatform's property exactly; a callable fake would accept an
    # invocation that the real protocol rejects.
    class _Manager:
        @property
        def deployment_settings(self) -> Any:
            return SimpleNamespace(mcp_proxy_base_url=base_url)

    return _Manager()


def test_the_box_is_created_knowing_where_to_send_its_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every name the mirror reads is filled here — one box, two sides."""

    from types import SimpleNamespace

    from astrabox.core.service.orchestrator.engine import transcript_mirror

    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED", "false")
    env = transcript_mirror.mirror_env(
        _manager("http://backend.test/"),
        "sess-1",
        SimpleNamespace(cwd="/workspace"),
    )
    assert env[transcript_mirror.TRANSCRIPT_BASE_URL_ENV] == "http://backend.test"
    assert env[transcript_mirror.PLATFORM_SESSION_ID_ENV] == "sess-1"
    assert env[transcript_mirror.TRANSCRIPT_PROJECT_KEY_ENV] == "/workspace"

    # The mirror reads exactly these names; a rename on either side is a
    # transcript that never leaves the box.
    module = _load(monkeypatch, Path("/nonexistent"), Path("/nonexistent"))
    program = _PROGRAM.read_text("utf-8")
    for name in env:
        assert f'"{name}"' in program, f"{name} is not read by the mirror"
    assert module.SUBPATH_PREFIX == "codex/"


def test_a_deployment_with_no_reachable_backend_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused at box create, where it names the deployment gap.

    Creating the box anyway defers the discovery to whenever someone tries to
    resume the conversation in a new one — by which time the history the resume
    needed was never written down.
    """

    from types import SimpleNamespace

    from astrabox.common.utils.errors import APIError
    from astrabox.core.service.orchestrator.engine import transcript_mirror

    with pytest.raises(APIError) as caught:
        transcript_mirror.mirror_env(
            _manager("   "), "sess-1", SimpleNamespace(cwd="/workspace")
        )
    assert caught.value.code == "AGENT_RUNTIME_ERROR"
    assert "mirror" in str(caught.value.message)


# ── the return trip: putting a conversation back into a replacement box ──────


class _CapturingFiles:
    def __init__(self) -> None:
        self.dirs: list[str] = []
        self.written: list[dict[str, Any]] = []

    async def create_directories(self, entries: list) -> None:
        self.dirs.extend(e.path for e in entries)

    async def write_file(self, path: str, data: bytes, **kw: Any) -> None:
        self.written.append({"path": path, "data": data, **kw})


class _CapturingSandbox:
    def __init__(self) -> None:
        self.files = _CapturingFiles()


def _fake_repository(monkeypatch: pytest.MonkeyPatch, scopes: list, entries: dict) -> None:
    from astrabox.persistence.repository import transcript_entry_repository as repo_mod

    class _Repo:
        async def list_scopes_by_platform_session(self, sid: str) -> list:
            return scopes

        async def load_subpath_entries_by_platform_session(
            self, sid: str, *, subpath: str | None
        ) -> list:
            return entries.get(subpath, [])

    monkeypatch.setattr(repo_mod, "TranscriptEntryRepository", _Repo)


async def _restore(monkeypatch: pytest.MonkeyPatch, scopes: list, entries: dict) -> Any:
    from astrabox.core.service.orchestrator.engine import transcript_mirror

    _fake_repository(monkeypatch, scopes, entries)
    sandbox = _CapturingSandbox()
    written = await transcript_mirror.restore_mirrored_logs(
        sandbox, "sess-1", namespace="codex/", root=_ROOT
    )
    return sandbox, written


@pytest.mark.asyncio
async def test_each_rollout_returns_to_the_path_its_scope_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subpath IS the path under `sessions/`, so restoring is transcription.

    Codex finds a thread by the uuid in the filename and a subagent's thread
    from its own file, so a rollout written anywhere else is a box that resumes
    into `no rollout found`.
    """

    main = "codex/2026/08/18/rollout-a-0000.jsonl"
    sub = "codex/2026/08/18/rollout-b-1111.jsonl"
    sandbox, written = await _restore(
        monkeypatch,
        [{"subpath": main}, {"subpath": sub}],
        {main: [{"type": "session_meta"}, {"type": "response_item"}], sub: [{"type": "x"}]},
    )
    assert written == 2
    assert [w["path"] for w in sandbox.files.written] == [
        f"{_ROOT}/2026/08/18/rollout-a-0000.jsonl",
        f"{_ROOT}/2026/08/18/rollout-b-1111.jsonl",
    ]
    assert sandbox.files.dirs == [f"{_ROOT}/2026/08/18"] * 2


@pytest.mark.asyncio
async def test_the_restored_file_is_one_json_object_per_line_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rollout's order is its meaning; its bytes are not.

    The store's contract is deep equality, and Codex parses each line rather
    than diffing it, so re-serializing is fine — but a reordered file is a
    different conversation.
    """

    scope = "codex/a.jsonl"
    given = [{"type": "session_meta"}, {"type": "response_item", "payload": {"n": 1}},
             {"type": "event_msg", "payload": {"t": "ü"}}]
    sandbox, _ = await _restore(monkeypatch, [{"subpath": scope}], {scope: given})
    body = sandbox.files.written[0]["data"].decode("utf-8")
    assert body.endswith("\n")
    assert [json.loads(line) for line in body.splitlines()] == given


@pytest.mark.asyncio
async def test_a_rollout_is_owned_by_the_account_that_appends_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app-server opens this file to write the next turn.

    A root-owned rollout resumes and then fails on the first thing the thread
    records — the box looks healthy right up to the point it stops being.
    """

    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        SANDBOX_IMAGE_WORKLOAD_USER,
    )

    scope = "codex/a.jsonl"
    sandbox, _ = await _restore(monkeypatch, [{"subpath": scope}], {scope: [{"n": 1}]})
    assert sandbox.files.written[0]["owner"] == SANDBOX_IMAGE_WORKLOAD_USER
    assert sandbox.files.written[0]["group"] == SANDBOX_IMAGE_WORKLOAD_USER


@pytest.mark.asyncio
async def test_another_engines_scope_in_the_same_session_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store is shared; the namespace is what keeps the engines apart."""

    sandbox, written = await _restore(
        monkeypatch,
        [{"subpath": None}, {"subpath": "subagents/agent-7"}, {"subpath": "codex/a.jsonl"}],
        {"codex/a.jsonl": [{"n": 1}]},
    )
    assert written == 1
    assert sandbox.files.written[0]["path"].endswith("/a.jsonl")


@pytest.mark.asyncio
async def test_a_session_with_nothing_mirrored_restores_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero is the signal the start flow drops the resume key on.

    Nothing was mirrored for this conversation, so there is no file for the
    engine to find and no history to lose. The caller therefore starts a new
    conversation in the box instead of naming a key the engine would refuse in
    its own words (`no rollout found for thread id`), which would end the
    session rather than answer the next message.
    `tests/engine_provisioning_contract_test.py` holds that decision.
    """

    sandbox, written = await _restore(monkeypatch, [], {})
    assert written == 0
    assert sandbox.files.written == []


# ── the declaration and the image that serves it ─────────────────────────────


def _register_engines() -> None:
    """Registration is a side effect of importing the platform; do not rely on
    another test module having caused it first."""

    from astrabox.providers import register_builtin_providers

    register_builtin_providers()


#: The image that serves each engine's declaration. Hand-listed because a
#: Dockerfile path is not derivable from an engine kind; held to the registry by
#: the test below, so an engine that declares a session log without appearing
#: here fails instead of going unchecked.
_ENGINE_IMAGE = {
    "codex": "containers/sandbox-codex/Dockerfile",
    "deepseek_harness": "containers/sandbox-deepseek-harness/Dockerfile",
    "pi": "containers/sandbox-pi/Dockerfile",
}


def _engines_declaring_a_session_log() -> set[str]:
    """Every registered engine that asks the platform to move its transcript."""

    from astrabox.core.service.orchestrator.engine.registry import (
        get_engine_adapter,
        known_engine_kinds,
    )

    _register_engines()
    return {
        kind
        for kind in known_engine_kinds()
        if get_engine_adapter(kind).capabilities.session_log is not None
    }


def test_every_engine_declaring_a_session_log_names_its_image() -> None:
    """The engine set comes from the registry, so the next engine is covered.

    Without this the two checks below are only as complete as the list above:
    an engine could declare a session log, have its declaration mirrored and
    restored by the platform, and never have its image checked for the relay or
    for the environment that aims it. That box comes up healthy, streams turns,
    and loses the conversation.
    """

    engines = _engines_declaring_a_session_log()
    assert engines, "no engine declares a session log any more"
    assert engines == set(_ENGINE_IMAGE), (
        "every engine declaring a session log needs its image listed here: "
        f"registry={sorted(engines)} listed={sorted(_ENGINE_IMAGE)}"
    )


@pytest.mark.parametrize(("engine_kind", "dockerfile"), sorted(_ENGINE_IMAGE.items()))
def test_what_an_engine_declares_is_what_its_image_tells_the_relay(
    engine_kind: str, dockerfile: str
) -> None:
    """One contract, written on both sides of a box.

    The adapter's declaration is what the platform mirrors and restores by; the
    image's environment is what the in-box relay reads. They are the same two
    values in two files, and a rename on either side would mirror the wrong
    tree — or none — while every box still came up healthy and every turn still
    streamed. Nothing observes the disagreement until a replacement box is
    asked to rejoin a conversation that was never written down.
    """

    from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter

    _register_engines()
    declared = get_engine_adapter(engine_kind).capabilities.session_log
    assert declared is not None, f"{engine_kind} declares no session log"

    text = (Path(__file__).resolve().parents[1] / dockerfile).read_text("utf-8")
    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        SANDBOX_IMAGE_WORKLOAD_HOME,
    )

    for name, value in (
        # The image pins the mirror root at build time, before any Session
        # exists, so what it can pin is the template rendered for the image
        # account — the conversation tenancy's home. A shared box needs
        # per-conversation mirror roots, which is the phase-2 instantiation
        # work, not the image's build-time claim.
        (
            "ASTRABOX_TRANSCRIPT_MIRROR_ROOT",
            declared.rendered_root(home=SANDBOX_IMAGE_WORKLOAD_HOME),
        ),
        ("ASTRABOX_TRANSCRIPT_MIRROR_NAMESPACE", declared.namespace),
        ("ASTRABOX_TRANSCRIPT_MIRROR_GLOB", declared.glob),
    ):
        assert f"{name}={value}" in text, (
            f"{dockerfile} does not set {name}={value}, which is what "
            f"{engine_kind} declares"
        )


def test_pi_resume_needs_a_workspace_that_does_not_move_between_boxes() -> None:
    """A restored pi log is only found when the box's cwd matches its header.

    The platform transcribes a session log back under the path its scope names,
    header line included, and pi resolves `--session <id>` by listing its
    session directory and keeping the files whose header `cwd` equals the
    process cwd — `SessionManager.list` filters on that whenever
    `--session-dir` is given, which this adapter always gives. A workspace that
    differed between boxes would push the resolver to its cross-project branch,
    which asks the operator y/N on stdin; in rpc mode stdin is the command
    channel, so pi would consume the platform's first command as an answer and
    exit. Constant templates are what keep the restored file resolvable.
    """

    from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter

    _register_engines()
    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        composed_runtime_profile,
    )

    profile = composed_runtime_profile("pi", "conversation")
    for field in ("home_template", "workspace_template"):
        value = str(getattr(profile, field))
        assert "{" not in value, (
            f"pi's {field} is {value!r}; a per-session path leaves a "
            "restored session log unresolvable in a replacement box"
        )


def test_every_such_image_installs_the_one_relay() -> None:
    """Many images, one program: the mechanism is not copied per engine.

    What differs between them is the environment naming their engine's files. A
    second copy would drift on the half nobody looks at — the batch identity
    that makes a restart re-send rather than duplicate.
    """

    root = Path(__file__).resolve().parents[1]
    missing = [
        dockerfile
        for dockerfile in sorted(_ENGINE_IMAGE.values())
        if "runtime/astrabox-transcript-mirror"
        not in (root / dockerfile).read_text("utf-8")
    ]
    assert not missing, f"{missing} do not install the shared relay"
