"""The container entry point: which shape it takes, and how it fails.

Three things are worth nailing here, and they are the three that would cost a
deployment real time if they broke:

1. **The derivation.** There is no enable flag, so the decision to supervise is
   read off the backend name and the base URL. Every combination is asserted,
   because the wrong answer is either "no sandbox server in a deployment that
   needs one" or "a competing server next to the operator's own".
2. **The exec path stays a real exec.** The no-supervision shape must not gain a
   wrapper process, so that branch is asserted to call ``execvp`` and to reach
   nothing after it.
3. **Startup failures carry the server's own output.** A server that cannot
   start says why on its stderr; a supervisor that swallowed that would leave the
   operator reading "unhealthy" with no cause.

Real subprocesses are used wherever the behaviour under test IS process
behaviour (a child that exits, a child that ignores its output stream), because a
mocked Popen cannot fail the way a process fails. Nothing here starts a
container or a real sandbox server.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import yaml

from astrabox.common.utils.settings import AstraBoxRuntimeSettings
from astrabox.deploy import onebox

#: The sandbox reach mode AstraBox reads; the entry point must not set it.
RELAY_ENV = "ASTRABOX_SANDBOX_ENDPOINT_VIA_SERVER_PROXY"

_ALL_ENV = (
    onebox.BACKEND_ENV,
    onebox.BASE_URL_ENV,
    RELAY_ENV,
    onebox.START_TIMEOUT_ENV,
    onebox.MODEL_PROVIDER_ENV,
    onebox.LITELLM_MASTER_KEY_ENV,
    onebox.CREDENTIAL_VAULT_ENV,
    onebox.EGRESS_DNS_UPSTREAM_ENV,
    onebox.EGRESS_DNS_UPSTREAM_DEFAULT_ENV,
    onebox.GATEWAY_DNS_ADDRESS_ENV,
    onebox.SANDBOX_EDGE_SERVICE_ENV,
    onebox.SANDBOX_DNS_EDGE_SERVICE_ENV,
    onebox.SANDBOX_EDGE_CALLBACK_PORT_ENV,
    onebox.SANDBOX_EDGE_NETWORK_ENV,
    onebox.EGRESS_DENY_CIDRS_ENV,
    onebox.MCP_PROXY_BASE_URL_ENV,
    onebox.ANTHROPIC_API_KEY_ENV,
    onebox.ANTHROPIC_AUTH_TOKEN_ENV,
    onebox.ANTHROPIC_BASE_URL_ENV,
    onebox.ANTHROPIC_MODEL_ENV,
    onebox.ASTRABOX_DB_URL_ENV,
    onebox.ASTRABOX_DB_PASSWORD_FILE_ENV,
    onebox.ASTRABOX_DB_HOST_ENV,
    onebox.ASTRABOX_DB_PORT_ENV,
    onebox.LITELLM_DATABASE_URL_ENV,
    onebox.LITELLM_DATABASE_PASSWORD_FILE_ENV,
    onebox.LITELLM_DATABASE_HOST_ENV,
    onebox.LITELLM_DATABASE_PORT_ENV,
    onebox.CHANNEL_GATEWAY_BASE_URL_ENV,
    onebox.CHANNEL_GATEWAY_TOKEN_ENV,
    onebox.CHANNEL_GATEWAY_HOST_ENV,
    onebox.CHANNEL_GATEWAY_PORT_ENV,
    onebox.CHANNEL_GATEWAY_MANIFEST_ENV,
    "DEEPSEEK_API_KEY",
    onebox.TITLE_MODEL_NAME_ENV,
    onebox.TITLE_MODEL_BASE_URL_ENV,
    "ASTRABOX_LITELLM_BASE_URL",
    "ASTRABOX_LITELLM_API_KEY",
    "ASTRABOX_STATE_DIR",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from astrabox.config.settings import get_settings

    get_settings.cache_clear()
    original = {name: os.environ.get(name) for name in _ALL_ENV}
    for name in _ALL_ENV:
        monkeypatch.delenv(name, raising=False)
    # The sandbox-server-shape cases isolate that leg: an external gateway
    # address keeps LiteLLM startup outside their frame.
    monkeypatch.setenv("ASTRABOX_LITELLM_BASE_URL", "http://external-gateway.test:4000")
    monkeypatch.setenv(onebox.LITELLM_API_KEY_ENV_NAME, "sk-external-scoped")
    monkeypatch.setenv(
        onebox.CHANNEL_GATEWAY_BASE_URL_ENV,
        "https://channels.example.test",
    )
    monkeypatch.setenv(onebox.CHANNEL_GATEWAY_TOKEN_ENV, "c" * 32)
    yield

    # Several functions under test deliberately write straight to os.environ.
    # When a key was absent above, monkeypatch.delenv had nothing to record and
    # therefore cannot undo a later raw write. Restore the process environment
    # explicitly so this module cannot change the posture of tests collected
    # after it in the same pytest process.
    for name, value in original.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 1. which shape the container takes
# ---------------------------------------------------------------------------


def test_an_unset_backend_needs_a_sandbox_server() -> None:
    # Unset must resolve the way the app resolves it, or the container boots on
    # open_sandbox with nothing to talk to. Pinned against Settings below.
    assert onebox.needs_sandbox_server() is True


def test_the_unset_default_tracks_the_apps_own_default() -> None:
    from astrabox.config.settings import AstraBoxSettings

    assert AstraBoxSettings.model_fields["sandbox_backend"].default == onebox.OPEN_SANDBOX_BACKEND


def test_a_third_party_backend_needs_no_sandbox_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A backend supplied by a downstream distribution brings its own control
    # plane; this module must not start a lifecycle server beside it.
    monkeypatch.setenv(onebox.BACKEND_ENV, "vendor_cloud")
    assert onebox.needs_sandbox_server() is False


def test_open_sandbox_without_a_base_url_needs_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(onebox.BACKEND_ENV, "open_sandbox")
    assert onebox.needs_sandbox_server() is True


def test_the_removed_local_backend_name_still_starts_the_replacement_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.BACKEND_ENV, "direct_docker")
    assert onebox.needs_sandbox_server() is True


def test_settings_normalize_the_removed_local_backend_name() -> None:
    from astrabox.config.settings import AstraBoxSettings

    assert AstraBoxSettings(sandbox_backend="direct_docker").sandbox_backend == "open_sandbox"


def test_open_sandbox_with_an_operators_own_server_needs_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole reason the decision is derived rather than flagged: an operator
    # who runs their own lifecycle server must not get a second one started
    # beside it, competing for the same Docker daemon.
    monkeypatch.setenv(onebox.BACKEND_ENV, "open_sandbox")
    monkeypatch.setenv(onebox.BASE_URL_ENV, "http://opensandbox.internal:8080")
    assert onebox.needs_sandbox_server() is False


@pytest.mark.parametrize("value", ["OPEN_SANDBOX", "Open_Sandbox"])
def test_backend_name_is_matched_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(onebox.BACKEND_ENV, value)
    assert onebox.needs_sandbox_server() is True


def test_a_blank_base_url_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty or whitespace value is what an unset compose variable expands to;
    # treating it as "configured" would start no server and then fail in the
    # provider with a config error instead.
    monkeypatch.setenv(onebox.BACKEND_ENV, "open_sandbox")
    monkeypatch.setenv(onebox.BASE_URL_ENV, "   ")
    assert onebox.needs_sandbox_server() is True


# ---------------------------------------------------------------------------
# 2. the exec path
# ---------------------------------------------------------------------------


def test_without_a_server_to_run_it_execs_astrabox_and_returns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-supervision shape must not gain a supervisor process."""
    # An operator running their own lifecycle server: nothing to supervise here.
    monkeypatch.setenv(onebox.BASE_URL_ENV, "http://opensandbox.internal:8080")
    calls: list[tuple[str, list[str]]] = []

    def _fake_execvp(file: str, args: list[str]) -> None:
        calls.append((file, list(args)))
        raise SystemExit(0)  # stand in for "this process is replaced"

    monkeypatch.setattr(onebox.os, "execvp", _fake_execvp)
    # Nothing may be spawned on this path.
    monkeypatch.setattr(onebox, "_spawn_sandbox_server", lambda: pytest.fail("spawned a server"))
    monkeypatch.setattr(
        onebox,
        "ensure_sandbox_inference_key",
        lambda: pytest.fail("replaced an operator-supplied inference key"),
    )
    with pytest.raises(SystemExit):
        onebox.main()
    assert calls == [("astrabox", ["astrabox", "serve"])]


def test_external_litellm_provisions_its_sandbox_key_before_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An external gateway needs the same startup contract as the embedded one."""

    monkeypatch.setenv(onebox.BASE_URL_ENV, "http://opensandbox.internal:8080")
    monkeypatch.delenv(onebox.LITELLM_API_KEY_ENV_NAME, raising=False)
    calls: list[str] = []

    monkeypatch.setattr(
        onebox,
        "ensure_shared_identity_key",
        lambda: calls.append("identity") or "shared-secret",
    )
    monkeypatch.setattr(
        onebox,
        "ensure_sandbox_inference_key",
        lambda: calls.append("sandbox-key") or "sk-scoped",
    )

    def _fake_execvp(_file: str, _args: list[str]) -> None:
        calls.append("exec")
        raise SystemExit(0)

    monkeypatch.setattr(onebox.os, "execvp", _fake_execvp)

    with pytest.raises(SystemExit):
        onebox.main()

    assert calls == ["identity", "sandbox-key", "exec"]


def test_the_exec_path_forwards_a_custom_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(onebox.BASE_URL_ENV, "http://opensandbox.internal:8080")
    calls: list[tuple[str, list[str]]] = []

    def _fake_execvp(file: str, args: list[str]) -> None:
        calls.append((file, list(args)))
        raise SystemExit(0)

    monkeypatch.setattr(onebox.os, "execvp", _fake_execvp)
    with pytest.raises(SystemExit):
        onebox.main(["astrabox", "--help"])
    assert calls == [("astrabox", ["astrabox", "--help"])]


# ---------------------------------------------------------------------------
# 3. bringing the server up
# ---------------------------------------------------------------------------


def _python_child(script: str) -> onebox._Child:
    """A real child process running ``script``, with its output pumped."""
    return onebox._Child("sandbox-server", (sys.executable, "-c", script), prefix_output=True)


@pytest.fixture
def child_reaper() -> Iterator[list[onebox._Child]]:
    """Guarantee every spawned child is gone when the test ends."""
    spawned: list[onebox._Child] = []
    yield spawned
    for child in spawned:
        child.stop()


def test_a_server_that_dies_reports_its_own_output(
    child_reaper: list[onebox._Child],
) -> None:
    # The likeliest failure in this position (unwritable metadata dir, a moved
    # upstream constant, no Docker socket) exits fast with the reason on stderr.
    # That reason is the whole value of the message.
    child = _python_child(
        "import sys; print('cannot write /data/opensandbox', file=sys.stderr); sys.exit(3)"
    )
    child_reaper.append(child)
    with pytest.raises(onebox.OneBoxError) as err:
        onebox.wait_until_healthy(child, "http://127.0.0.1:1/health", timeout=10)
    message = str(err.value)
    assert "exited with code 3" in message
    assert "cannot write /data/opensandbox" in message


def test_a_server_that_never_answers_times_out_and_is_stopped(
    child_reaper: list[onebox._Child],
) -> None:
    # A live process that never serves: the bound is what stops the container
    # hanging forever, and the child must not be left running behind it.
    child = _python_child("import sys, time; print('starting', flush=True); time.sleep(120)")
    child_reaper.append(child)
    started = time.monotonic()
    with pytest.raises(onebox.OneBoxError) as err:
        onebox.wait_until_healthy(child, "http://127.0.0.1:1/health", timeout=0.75)
    elapsed = time.monotonic() - started
    assert elapsed < 30, "the wait must be bounded, not just eventually give up"
    assert "did not answer" in str(err.value)
    assert onebox.START_TIMEOUT_ENV in str(err.value)
    assert "starting" in str(err.value), "the server's output belongs in the message"
    assert child.process.poll() is not None, "a timed-out server must not be left running"


def test_a_healthy_server_returns_immediately(
    monkeypatch: pytest.MonkeyPatch, child_reaper: list[onebox._Child]
) -> None:
    child = _python_child("import time; time.sleep(120)")
    child_reaper.append(child)
    monkeypatch.setattr(onebox, "_health_probe", lambda url: True)
    onebox.wait_until_healthy(child, "http://127.0.0.1:1/health", timeout=5)
    assert child.process.poll() is None, "a healthy server keeps running"


def test_health_probe_treats_every_transport_failure_as_not_yet() -> None:
    # Port 1 on loopback refuses; a refusal during startup is normal, not fatal.
    assert onebox._health_probe("http://127.0.0.1:1/health") is False


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.2.3"])
def test_start_timeout_refuses_a_value_that_is_not_a_positive_number(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(onebox.START_TIMEOUT_ENV, value)
    with pytest.raises(onebox.OneBoxError, match=onebox.START_TIMEOUT_ENV):
        onebox.start_timeout_seconds()


def test_start_timeout_defaults_and_accepts_an_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert onebox.start_timeout_seconds() == float(onebox.DEFAULT_START_TIMEOUT_SECONDS)
    monkeypatch.setenv(onebox.START_TIMEOUT_ENV, "5")
    assert onebox.start_timeout_seconds() == 5.0


# ---------------------------------------------------------------------------
# 4. wiring AstraBox at the server it just started
# ---------------------------------------------------------------------------


def test_backend_wiring_points_at_the_launchers_own_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Composed by the launcher module, never spelled again here: the address the
    # server binds and the address AstraBox dials cannot be allowed to drift.
    from astrabox.deploy import sandbox_server

    monkeypatch.setenv(sandbox_server.SERVER_PORT_ENV, "9111")
    base_url = onebox._export_backend_wiring()
    assert base_url == "http://127.0.0.1:9111"
    assert onebox.os.environ[onebox.BASE_URL_ENV] == base_url


def test_backend_wiring_reaches_sandboxes_directly() -> None:
    # The bundled server shares this process's network namespace, so its relay
    # could only dial the published address AstraBox can dial itself, and the
    # relay drops the Cookie and rewrites the Host that the DeepSeek Harness
    # and Hermes in-box services authenticate with.
    onebox._export_backend_wiring()
    assert RELAY_ENV not in onebox.os.environ
    assert AstraBoxRuntimeSettings().sandbox_endpoint_via_server_proxy is False


# ---------------------------------------------------------------------------
# 5. supervision
# ---------------------------------------------------------------------------


def test_astrabox_exiting_stops_the_server_and_sets_the_exit_code(
    child_reaper: list[onebox._Child],
) -> None:
    server = _python_child("import time; time.sleep(120)")
    app = onebox._Child(
        "astrabox", (sys.executable, "-c", "raise SystemExit(7)"), prefix_output=False
    )
    child_reaper.extend([server, app])
    # AstraBox's status is the one an operator reads, so `_supervise` returns
    # it as its own exit code.
    assert onebox._supervise(app, [server]) == 7
    assert server.process.poll() is not None, "the server must not outlive astrabox"


def test_a_dying_server_takes_astrabox_down_with_it(
    child_reaper: list[onebox._Child],
) -> None:
    server = _python_child(
        "import sys; print('lost the docker socket', file=sys.stderr); sys.exit(0)"
    )
    app = onebox._Child(
        "astrabox", (sys.executable, "-c", "import time; time.sleep(120)"), prefix_output=False
    )
    child_reaper.extend([server, app])
    # Note the exit code: the server left CLEANLY, but a container whose sandbox
    # control plane is gone is broken, so success is not reportable. Exiting is
    # also what lets the runtime's restart policy act.
    assert onebox._supervise(app, [server]) == 1
    assert app.process.poll() is not None, "astrabox must not be left serving without a backend"


def test_astrabox_killed_by_a_signal_reports_the_signal_not_a_wrapped_status(
    child_reaper: list[onebox._Child],
) -> None:
    # A crash must stay a failure, and read as one: Popen reports SIGKILL as
    # -9, which sys.exit would turn into 247. The container reports 137, the
    # status a shell and tini give a SIGKILL.
    server = _python_child("import time; time.sleep(120)")
    app = onebox._Child(
        "astrabox",
        (sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"),
        prefix_output=False,
    )
    child_reaper.extend([server, app])
    assert onebox._supervise(app, [server]) == 128 + signal.SIGKILL
    assert server.process.poll() is not None


def test_a_moved_sandbox_edge_restarts_the_container(
    child_reaper: list[onebox._Child], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every sandbox is given the edge addresses read at startup; an edge Docker
    # restarted at another address would leave all of them timing out, so the
    # container ends (status 1) and its restart policy reads them again.
    monkeypatch.setattr(onebox, "_EDGE_WATCH_INTERVAL_SECONDS", 0.0)
    addresses = {"sandbox-edge": "172.17.0.10"}
    monkeypatch.setattr(onebox, "_detect_edge_ip", lambda service, owner: addresses[service])
    server = _python_child("import time; time.sleep(120)")
    app = onebox._Child(
        "astrabox", (sys.executable, "-c", "import time; time.sleep(120)"), prefix_output=False
    )
    child_reaper.extend([server, app])
    watch = onebox._edge_address_watch({"sandbox-edge": "172.17.0.10"})
    assert watch() is None
    addresses["sandbox-edge"] = "172.17.0.4"
    assert onebox._supervise(app, [server], watch=watch) == 1
    assert app.process.poll() is not None
    assert server.process.poll() is not None


def test_an_edge_that_is_not_running_is_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    def _not_running(service: str, owner: object) -> str:
        raise onebox.OneBoxError(f"{service} has no running container")

    monkeypatch.setattr(onebox, "_detect_edge_ip", _not_running)
    assert onebox._edge_address_watch({"sandbox-dns-edge": "172.17.0.9"})() is None


@pytest.fixture
def restore_signal_handlers() -> Iterator[None]:
    """Put the interpreter's SIGTERM/SIGINT handlers back afterwards.

    ``_forward_signals`` installs process-wide handlers. Leaving them in place
    would hand every later test in the session this module's shutdown behaviour,
    and would make a Ctrl-C during the run signal long-dead children.
    """
    saved = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)


def _signal_recorder(
    log: Path, name: str, signals: dict[str, int], *, ready_file: Path | None = None
) -> str:
    """A child script that appends ``name:SIGNAL`` to ``log`` and exits.

    Each handler also appends ``name:exit`` after a short pause, so the log
    shows whether the next child was signalled before this one had finished.
    The child announces that its handlers are installed on its pumped output,
    or, unpumped like AstraBox, by creating ``ready_file``: a child signalled
    before that point takes the default action and records nothing.
    """
    handlers = "".join(
        f"signal.signal(signal.{number}, lambda *a: finish({number!r}, {code}))\n"
        for number, code in signals.items()
    )
    announce = (
        f"pathlib.Path({str(ready_file)!r}).write_text('ready')\n"
        if ready_file is not None
        else "print('ready', flush=True)\n"
    )
    return (
        "import os, pathlib, signal, sys, time\n"
        "def record(line):\n"
        f"    fd = os.open({str(log)!r}, os.O_WRONLY | os.O_APPEND | os.O_CREAT)\n"
        "    os.write(fd, (line + '\\n').encode())\n"
        "    os.close(fd)\n"
        "def finish(number, code):\n"
        f"    record({name!r} + ':' + number[3:])\n"
        "    time.sleep(0.3)\n"
        f"    record({name!r} + ':exit')\n"
        "    sys.exit(code)\n"
        f"{handlers}{announce}"
        "time.sleep(120)\n"
    )


def test_a_signal_stops_astrabox_first_then_the_sidecars_then_the_foundation_in_order(
    child_reaper: list[onebox._Child], restore_signal_handlers: None, tmp_path: Path
) -> None:
    # SIGTERM must arrive at AstraBox for its lifespan shutdown hook to run at
    # all, and it must arrive while the services that hook calls are still up.
    # The database outlives every client: PostgreSQL gets SIGINT (fast
    # shutdown; SIGTERM would wait for clients) only after the sidecars have
    # exited, and the pool store goes last.
    log = tmp_path / "shutdown.log"
    ready_file = tmp_path / "astrabox-handler-installed"
    app = onebox._Child(
        "astrabox",
        (
            sys.executable,
            "-c",
            _signal_recorder(log, "astrabox", {"SIGTERM": 0}, ready_file=ready_file),
        ),
        prefix_output=False,
    )
    server = _python_child(_signal_recorder(log, "sandbox-server", {"SIGTERM": 0}))
    postgres = onebox._start_foundation(
        onebox.FoundationService(
            "postgres",
            (
                sys.executable,
                "-c",
                _signal_recorder(log, "postgres", {"SIGINT": 0, "SIGTERM": 9}),
            ),
            ready=lambda: True,
            stop_signal=signal.SIGINT,
        )
    )
    valkey = onebox._start_foundation(
        onebox.FoundationService(
            "valkey",
            (sys.executable, "-c", _signal_recorder(log, "valkey", {"SIGTERM": 0})),
            ready=lambda: True,
        )
    )
    child_reaper.extend([app, server, postgres, valkey])
    for child in (server, postgres, valkey):
        _await_line(child, "ready")
    _await_file(ready_file)
    onebox._forward_signals(app)

    os.kill(os.getpid(), signal.SIGTERM)
    assert onebox._supervise(app, [server], [postgres, valkey]) == 0

    assert log.read_text(encoding="utf-8").splitlines() == [
        "astrabox:TERM",
        "astrabox:exit",
        "sandbox-server:TERM",
        "sandbox-server:exit",
        "postgres:INT",
        "postgres:exit",
        "valkey:TERM",
        "valkey:exit",
    ]


def test_a_dying_foundation_service_takes_the_container_down_in_order(
    child_reaper: list[onebox._Child], tmp_path: Path
) -> None:
    # A database that exits is a broken container even when it exited cleanly;
    # no restart in place. Its clients stop first, the other foundation last.
    log = tmp_path / "shutdown.log"
    ready_file = tmp_path / "astrabox-handler-installed"
    app = onebox._Child(
        "astrabox",
        (
            sys.executable,
            "-c",
            _signal_recorder(log, "astrabox", {"SIGTERM": 0}, ready_file=ready_file),
        ),
        prefix_output=False,
    )
    server = _python_child(_signal_recorder(log, "sandbox-server", {"SIGTERM": 0}))
    valkey = onebox._start_foundation(
        onebox.FoundationService(
            "valkey",
            (sys.executable, "-c", _signal_recorder(log, "valkey", {"SIGTERM": 0})),
            ready=lambda: True,
        )
    )
    child_reaper.extend([app, server, valkey])
    for child in (server, valkey):
        _await_line(child, "ready")
    _await_file(ready_file)
    postgres = onebox._start_foundation(
        onebox.FoundationService(
            "postgres",
            (sys.executable, "-c", "print('database files are gone', flush=True)"),
            ready=lambda: True,
            stop_signal=signal.SIGINT,
        )
    )
    child_reaper.append(postgres)

    assert onebox._supervise(app, [server], [postgres, valkey]) == 1
    assert log.read_text(encoding="utf-8").splitlines() == [
        "astrabox:TERM",
        "astrabox:exit",
        "sandbox-server:TERM",
        "sandbox-server:exit",
        "valkey:TERM",
        "valkey:exit",
    ]


def test_a_foundation_never_takes_the_exec_path_and_outlives_astrabox(
    monkeypatch: pytest.MonkeyPatch,
    child_reaper: list[onebox._Child],
    restore_signal_handlers: None,
    tmp_path: Path,
) -> None:
    # An operator's own lifecycle server leaves nothing else to supervise, which
    # alone would exec AstraBox. A foundation service rules that out: exec would
    # orphan the database and drop the data-volume lock the caller holds.
    monkeypatch.setenv(onebox.BASE_URL_ENV, "http://opensandbox.internal:8080")
    monkeypatch.setattr(onebox.os, "execvp", lambda *a: pytest.fail("exec'd astrabox"))
    log = tmp_path / "shutdown.log"
    started: list[str] = []

    def _fake_astrabox(argv: Any) -> onebox._Child:
        started.append("astrabox")
        assert log.exists(), "astrabox started before the foundation was ready"
        app = onebox._Child(
            "astrabox", (sys.executable, "-c", "raise SystemExit(0)"), prefix_output=False
        )
        child_reaper.append(app)
        return app

    monkeypatch.setattr(onebox, "_spawn_astrabox", _fake_astrabox)
    script = _signal_recorder(log, "postgres", {"SIGINT": 0, "SIGTERM": 9}).replace(
        "print('ready', flush=True)\n",
        f"pathlib.Path({str(log)!r}).touch()\nprint('ready', flush=True)\n",
    )
    service = onebox.FoundationService(
        "postgres",
        (sys.executable, "-c", script),
        ready=log.exists,
        stop_signal=signal.SIGINT,
    )
    real_start = onebox._start_foundation

    def _tracked_start(value: onebox.FoundationService) -> onebox._Child:
        child = real_start(value)
        child_reaper.append(child)
        return child

    monkeypatch.setattr(onebox, "_start_foundation", _tracked_start)

    assert onebox.main(["astrabox", "serve"], foundation=[service]) == 0
    assert started == ["astrabox"]
    assert log.read_text(encoding="utf-8").splitlines() == ["postgres:INT", "postgres:exit"]


def _await_file(path: Path, *, timeout: float = 30.0) -> None:
    """Wait for a child that has no pumped output to say it got somewhere."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def _await_line(child: onebox._Child, needle: str, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in child.output_tail():
            return
        time.sleep(0.05)
    raise AssertionError(f"{child.name} never printed {needle!r}")


def test_stop_escalates_to_kill_when_a_child_ignores_sigterm(
    monkeypatch: pytest.MonkeyPatch, child_reaper: list[onebox._Child]
) -> None:
    monkeypatch.setattr(onebox, "_CHILD_SHUTDOWN_GRACE_SECONDS", 0.5)
    child = _python_child(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(120)\n"
    )
    child_reaper.append(child)
    _await_line(child, "ready")
    child.stop()
    assert child.process.poll() is not None, "an unresponsive child must still be reaped"


def test_a_pumped_child_is_unbuffered_so_its_output_is_never_stranded(
    child_reaper: list[onebox._Child],
) -> None:
    """Reproduced from a real run: the server's logs vanished entirely.

    A pipe makes the child's stdout block-buffered, so log lines sit in a buffer
    that a SIGTERM or a crash never flushes — and that output is exactly what the
    fail-loud messages quote. Note the deliberate absence of ``flush=True``: this
    passes only because the child is spawned unbuffered.
    """
    child = _python_child("print('unflushed line')\nimport time\ntime.sleep(120)\n")
    child_reaper.append(child)
    _await_line(child, "unflushed line")
    assert child.process.poll() is None, "the line must arrive while the child still lives"


def test_pumped_third_party_output_is_redacted_before_stdout_and_tail(
    monkeypatch: pytest.MonkeyPatch,
    child_reaper: list[onebox._Child],
    capsys: pytest.CaptureFixture[str],
) -> None:
    inherited_secret = "provider-credential-value-12345"
    rejected_key = "sk-test-redaction-abcdef1234567890"
    key_hash = "a" * 47 + "2" + "b" * 16
    monkeypatch.setenv("ACME_ACCESS_TOKEN", inherited_secret)
    child = _python_child(
        "import time\n"
        f"print('opaque {inherited_secret}', flush=True)\n"
        f"print('Received API Key = {rejected_key}', flush=True)\n"
        f"print('hash={key_hash}', flush=True)\n"
        "time.sleep(120)\n"
    )
    child_reaper.append(child)
    _await_line(child, "hash=[redacted]")

    tail = child.output_tail()
    stdout = capsys.readouterr().out
    for secret in (inherited_secret, rejected_key, key_hash):
        assert secret not in tail
        assert secret not in stdout
    assert "opaque [redacted]" in tail
    assert "Received API Key = [redacted]" in tail


def test_the_relayed_deepseek_harness_launch_token_is_redacted(
    child_reaper: list[onebox._Child],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With sandbox endpoints relayed through the lifecycle server, the DeepSeek
    Harness adapter's token exchange is a request line in that server's access
    log. The line below is what opensandbox-server 0.2.3, started as onebox
    starts it, printed for that request, colour codes included. The token here
    is synthetic; the request shape matches the harness exchange."""

    token = "onebox-log-redaction-fixture-token-0000000000"
    line = (
        "\x1b[32mINFO\x1b[0m:     2026-09-23 10:05:55+0000 [-] uvicorn.access: "
        '127.0.0.1:54544 - "GET /sandboxes/0d4c2b1e-relay-probe/proxy/44780/'
        f'?token={token} HTTP/1.1" 404'
    )
    child = _python_child(f"import time\nprint({line!r}, flush=True)\ntime.sleep(120)\n")
    child_reaper.append(child)
    _await_line(child, "uvicorn.access")

    tail = child.output_tail()
    stdout = capsys.readouterr().out
    assert token not in tail
    assert token not in stdout
    assert "/proxy/44780/?token=[redacted] HTTP/1.1\" 404" in tail


def test_output_tail_is_bounded(child_reaper: list[onebox._Child]) -> None:
    # The tail exists for a failure message, so it must not grow without bound
    # for a server that logs steadily for weeks.
    child = _python_child(f"for i in range({onebox._OUTPUT_TAIL_LINES * 3}): print(i, flush=True)")
    child_reaper.append(child)
    child.process.wait(timeout=30)
    _await_line(child, str(onebox._OUTPUT_TAIL_LINES * 3 - 1))
    assert len(child.output_tail().splitlines()) <= onebox._OUTPUT_TAIL_LINES


def test_a_failure_after_the_server_starts_never_orphans_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server is a child PROCESS, not something Python cleans up.

    Reproduced from a real run: with an AstraBox command that does not exist, the
    spawn raised, the orchestrator died, and the healthy sandbox server was left
    running with the container's main process gone — holding its port and its
    Docker client.
    """
    monkeypatch.setenv(onebox.BACKEND_ENV, "open_sandbox")
    spawned: list[onebox._Child] = []

    def _fake_server() -> onebox._Child:
        child = _python_child("import time; time.sleep(120)")
        spawned.append(child)
        return child

    monkeypatch.setattr(onebox, "_spawn_sandbox_server", _fake_server)
    monkeypatch.setattr(onebox, "wait_until_healthy", lambda *a, **k: None)
    monkeypatch.setattr(onebox, "_export_backend_wiring", lambda: "http://127.0.0.1:8990")

    with pytest.raises(FileNotFoundError):
        onebox.main(["definitely-not-a-real-command-astrabox"])

    assert spawned, "the server should have been started"
    assert spawned[0].process.poll() is not None, "the server was orphaned"


def test_the_server_child_runs_this_interpreter_and_the_launcher_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rebinds must land in the process that serves, so ``-m`` is the shape.

    A console script or a shell would let a different interpreter (or a stale
    install) run the launcher, and the two module-constant rebinds only protect
    the process they happen in.
    """
    from astrabox.deploy import sandbox_server as launcher

    recorded: list[list[str]] = []

    class _Recorder:
        def __init__(self, name: str, argv: Any, *, prefix_output: bool) -> None:
            recorded.append(list(argv))
            self.process = subprocess.CompletedProcess(list(argv), 0)

    monkeypatch.setattr(onebox, "_Child", _Recorder)
    onebox._spawn_sandbox_server()
    assert recorded == [[sys.executable, "-m", "astrabox.deploy.sandbox_server"]]
    assert launcher.__name__ == "astrabox.deploy.sandbox_server"


# ---------------------------------------------------------------------------
# 5. the embedded channel gateway leg
# ---------------------------------------------------------------------------


def test_no_external_channel_gateway_selects_the_bundled_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(onebox.CHANNEL_GATEWAY_BASE_URL_ENV, raising=False)
    assert onebox.needs_channel_gateway() is True


def test_embedded_channel_gateway_wiring_is_private_and_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(onebox.CHANNEL_GATEWAY_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(onebox.CHANNEL_GATEWAY_TOKEN_ENV, raising=False)
    monkeypatch.setenv(onebox.CHANNEL_GATEWAY_PORT_ENV, "18765")

    assert onebox.ensure_channel_gateway_wiring() == "http://127.0.0.1:18765"
    assert len(os.environ[onebox.CHANNEL_GATEWAY_TOKEN_ENV]) >= 32
    assert (
        os.environ[onebox.CHANNEL_GATEWAY_MANIFEST_ENV]
        == onebox.CHANNEL_GATEWAY_MANIFEST_PATH
    )


def test_external_channel_gateway_refuses_plaintext_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        onebox.CHANNEL_GATEWAY_BASE_URL_ENV,
        "http://channels.example.test",
    )
    with pytest.raises(onebox.OneBoxError, match="must use HTTPS"):
        onebox.ensure_channel_gateway_wiring()


def test_channel_gateway_child_uses_the_baked_node_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[str, list[str], bool]] = []

    class _Recorder:
        def __init__(self, name: str, argv: Any, *, prefix_output: bool) -> None:
            recorded.append((name, list(argv), prefix_output))

    monkeypatch.setattr(onebox, "_Child", _Recorder)
    monkeypatch.setattr(onebox.os.path, "exists", lambda _path: True)
    onebox._spawn_channel_gateway()
    assert recorded == [
        (
            "channel-gateway",
            [onebox.CHANNEL_GATEWAY_BIN, onebox.CHANNEL_GATEWAY_SERVER_PATH],
            True,
        )
    ]


# ---------------------------------------------------------------------------
# Embedded LiteLLM gateway
# ---------------------------------------------------------------------------


def test_default_provider_needs_the_embedded_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    # This case exercises the embedded gateway, so remove the fixture's external one.
    monkeypatch.delenv("ASTRABOX_LITELLM_BASE_URL", raising=False)
    assert onebox.needs_litellm_gateway() is True


def test_embedded_gateway_maps_an_anthropic_compatible_upstream_to_a_stable_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.ANTHROPIC_AUTH_TOKEN_ENV, "upstream-token")
    monkeypatch.setenv(onebox.ANTHROPIC_BASE_URL_ENV, "https://models.example.test")
    monkeypatch.setenv(onebox.ANTHROPIC_MODEL_ENV, "provider-model-v2")

    onebox.ensure_litellm_provider_wiring()

    assert os.environ[onebox.ANTHROPIC_API_KEY_ENV] == "upstream-token"
    assert os.environ[onebox.ANTHROPIC_MODEL_ENV] == "anthropic/provider-model-v2"


def test_deepseek_provider_credential_does_not_override_the_agents_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator's Claude client default is not a platform routing policy.

    Agent authors select one of the concrete DeepSeek routes returned by model
    discovery. The deployment default is normalized for Claude Code without
    overriding the model stored on any Agent.
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv(onebox.ANTHROPIC_AUTH_TOKEN_ENV, "deepseek-token")
    monkeypatch.setenv(
        onebox.ANTHROPIC_BASE_URL_ENV,
        "https://api.deepseek.com/anthropic",
    )
    monkeypatch.setenv(onebox.ANTHROPIC_MODEL_ENV, "deepseek-chat")

    onebox.ensure_litellm_provider_wiring()

    assert os.environ["DEEPSEEK_API_KEY"] == "deepseek-token"
    assert os.environ[onebox.ANTHROPIC_MODEL_ENV] == "anthropic/deepseek-chat"


def test_deepseek_provider_credential_needs_no_anthropic_default_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv(onebox.ANTHROPIC_MODEL_ENV, raising=False)
    monkeypatch.setenv(onebox.ANTHROPIC_AUTH_TOKEN_ENV, "deepseek-token")
    monkeypatch.setenv(
        onebox.ANTHROPIC_BASE_URL_ENV,
        "https://api.deepseek.com/anthropic",
    )

    onebox.ensure_litellm_provider_wiring()

    assert os.environ["DEEPSEEK_API_KEY"] == "deepseek-token"
    assert onebox.ANTHROPIC_MODEL_ENV not in os.environ


def _bundled_route_for(requested: str) -> dict[str, str]:
    """The bundled gateway route LiteLLM would serve ``requested`` from."""

    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "containers" / "litellm" / "config.yaml").read_text(
            encoding="utf-8"
        )
    )
    for route in config["model_list"]:
        if fnmatch.fnmatchcase(requested, route["model_name"]):
            return dict(route["litellm_params"])
    raise AssertionError(f"the bundled gateway serves no route for {requested!r}")


@pytest.mark.parametrize("model", ["deepseek-flash", "anthropic/deepseek-v4-pro"])
def test_deepseek_labels_use_the_native_adapter_route(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """DeepSeek labels must reach the one adapter that disables its thinking.

    The Anthropic and OpenAI wire routes drop ``reasoning_effort: "none"``;
    LiteLLM's native DeepSeek adapter maps it to ``thinking: disabled``.
    """
    monkeypatch.setenv(onebox.ANTHROPIC_AUTH_TOKEN_ENV, "deepseek-token")
    monkeypatch.setenv(onebox.ANTHROPIC_BASE_URL_ENV, "https://api.deepseek.com/anthropic")
    monkeypatch.setenv(onebox.ANTHROPIC_MODEL_ENV, model)

    onebox.ensure_litellm_provider_wiring()

    upstream = model.removeprefix("anthropic/")
    title_route = os.environ[onebox.TITLE_MODEL_NAME_ENV]
    assert title_route == f"deepseek/{upstream}"
    served_by = _bundled_route_for(title_route)
    assert served_by["model"] == "deepseek/*"
    assert served_by["api_key"] == "os.environ/DEEPSEEK_API_KEY"
    assert os.environ[onebox.ANTHROPIC_MODEL_ENV] == f"anthropic/{upstream}"


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        (onebox.TITLE_MODEL_NAME_ENV, "operator-label-route"),
        (onebox.TITLE_MODEL_BASE_URL_ENV, "https://labels.example.test/v1"),
    ],
)
def test_deepseek_keeps_the_operators_title_settings(
    monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    monkeypatch.setenv(onebox.ANTHROPIC_AUTH_TOKEN_ENV, "deepseek-token")
    monkeypatch.setenv(onebox.ANTHROPIC_BASE_URL_ENV, "https://api.deepseek.com/anthropic")
    monkeypatch.setenv(onebox.ANTHROPIC_MODEL_ENV, "deepseek-flash")
    monkeypatch.setenv(setting, value)

    onebox.ensure_litellm_provider_wiring()

    assert os.environ.get(onebox.TITLE_MODEL_NAME_ENV) == (
        value if setting == onebox.TITLE_MODEL_NAME_ENV else None
    )


def test_other_upstreams_keep_the_deployment_default_title_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.ANTHROPIC_AUTH_TOKEN_ENV, "upstream-token")
    monkeypatch.setenv(onebox.ANTHROPIC_BASE_URL_ENV, "https://models.example.test")
    monkeypatch.setenv(onebox.ANTHROPIC_MODEL_ENV, "provider-model-v2")

    onebox.ensure_litellm_provider_wiring()

    assert onebox.TITLE_MODEL_NAME_ENV not in os.environ


def test_embedded_gateway_keeps_an_explicit_default_route_and_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.ANTHROPIC_AUTH_TOKEN_ENV, "unused-token")
    monkeypatch.setenv(onebox.ANTHROPIC_API_KEY_ENV, "chosen-key")
    monkeypatch.setenv(onebox.ANTHROPIC_BASE_URL_ENV, "https://models.example.test")
    monkeypatch.setenv(onebox.ANTHROPIC_MODEL_ENV, "anthropic/company-route")

    onebox.ensure_litellm_provider_wiring()

    assert os.environ[onebox.ANTHROPIC_API_KEY_ENV] == "chosen-key"
    assert os.environ[onebox.ANTHROPIC_MODEL_ENV] == "anthropic/company-route"


def test_default_protected_embedded_gateway_needs_private_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTRABOX_LITELLM_BASE_URL", raising=False)
    assert onebox.needs_gateway_dns() is True
    monkeypatch.setenv(onebox.CREDENTIAL_VAULT_ENV, "false")
    assert onebox.needs_gateway_dns() is False


def test_private_gateway_dns_never_points_sandboxes_at_this_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sandboxes may reach the address the gateway name resolves to; this
    # server's own address would open every port it listens on to them.
    monkeypatch.delenv(onebox.GATEWAY_DNS_ADDRESS_ENV, raising=False)
    with pytest.raises(onebox.OneBoxError, match=onebox.GATEWAY_DNS_ADDRESS_ENV):
        onebox.ensure_gateway_dns_wiring()
    assert onebox.GATEWAY_DNS_ADDRESS_ENV not in os.environ
    assert onebox.EGRESS_DNS_UPSTREAM_ENV not in os.environ


def test_private_gateway_dns_follows_the_given_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.GATEWAY_DNS_ADDRESS_ENV, "172.17.0.23")
    assert onebox.ensure_gateway_dns_wiring() == "172.17.0.23"
    assert os.environ[onebox.EGRESS_DNS_UPSTREAM_ENV] == "172.17.0.23:5353"


def test_compose_sandbox_edge_becomes_the_only_local_callback_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.SANDBOX_EDGE_SERVICE_ENV, "sandbox-edge")
    monkeypatch.setattr(
        onebox,
        "_detect_edge_ip",
        lambda service, owner: "172.17.0.23" if service == "sandbox-edge" else "",
    )

    assert onebox.ensure_sandbox_edge_wiring() == "172.17.0.23"
    assert os.environ[onebox.GATEWAY_DNS_ADDRESS_ENV] == "172.17.0.23"
    assert os.environ[onebox.MCP_PROXY_BASE_URL_ENV] == "http://172.17.0.23:8000"


def test_compose_sandbox_edge_keeps_explicit_operator_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.SANDBOX_EDGE_SERVICE_ENV, "sandbox-edge")
    monkeypatch.setenv(onebox.GATEWAY_DNS_ADDRESS_ENV, "198.51.100.8")
    monkeypatch.setenv(onebox.MCP_PROXY_BASE_URL_ENV, "https://callbacks.example.test")
    monkeypatch.setattr(
        onebox,
        "_detect_edge_ip",
        lambda _service, _owner: pytest.fail("explicit addresses must not inspect Docker"),
    )

    assert onebox.ensure_sandbox_edge_wiring() == "198.51.100.8"
    assert os.environ[onebox.MCP_PROXY_BASE_URL_ENV] == "https://callbacks.example.test"


def test_compose_dns_edge_becomes_the_only_sandbox_dns_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.SANDBOX_DNS_EDGE_SERVICE_ENV, "sandbox-dns-edge")
    monkeypatch.setattr(
        onebox,
        "_detect_edge_ip",
        lambda service, owner: "172.17.0.24" if service == "sandbox-dns-edge" else "",
    )

    assert onebox.ensure_sandbox_dns_edge_wiring() == "172.17.0.24"
    assert os.environ[onebox.EGRESS_DNS_UPSTREAM_ENV] == "172.17.0.24:53"


def test_compose_dns_edge_keeps_an_explicit_operator_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.SANDBOX_DNS_EDGE_SERVICE_ENV, "sandbox-dns-edge")
    monkeypatch.setenv(onebox.EGRESS_DNS_UPSTREAM_ENV, "192.0.2.53:5353")
    monkeypatch.setattr(
        onebox,
        "_detect_edge_ip",
        lambda _service, _owner: "172.17.0.24",
    )

    assert onebox.ensure_sandbox_dns_edge_wiring() == "172.17.0.24"
    assert os.environ[onebox.EGRESS_DNS_UPSTREAM_ENV] == "192.0.2.53:5353"



class _FakeContainer:
    def __init__(self, name: str, networks: dict[str, str], labels: dict[str, str]) -> None:
        self.name = name
        self.attrs: dict[str, Any] = {
            "Config": {"Labels": labels},
            "NetworkSettings": {
                "Networks": {net: {"IPAddress": ip} for net, ip in networks.items()}
            },
        }


class _FakeNetwork:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.labels = labels
        self.connected: list[str] = []

    def connect(self, container: _FakeContainer) -> None:
        self.connected.append(container.name)
        container.attrs["NetworkSettings"]["Networks"][self.name] = {"IPAddress": "10.9.0.9"}


def _labelled(labels: dict[str, str], wanted: list[str]) -> bool:
    """Docker's label filter: every ``key=value`` given must be on the resource."""
    return all(labels.get(key) == value for key, _, value in (item.partition("=") for item in wanted))


_COMPOSE = {
    "server": {"com.docker.compose.project": "proj"},
    "network": {
        "com.docker.compose.project": "proj",
        "com.docker.compose.network": "sandbox-edges",
    },
    "edge": lambda service: {
        "com.docker.compose.project": "proj",
        "com.docker.compose.service": service,
    },
}


class _FakeDocker:
    """The slice of docker-py onebox uses: labels, networks, the bridge IPAM."""

    def __init__(
        self,
        *,
        server_networks: tuple[str, ...],
        edge_networks: dict[str, tuple[str, ...]],
        bridge_subnets: tuple[str, ...] = ("172.17.0.0/16",),
        labels: dict[str, Any] = _COMPOSE,
    ) -> None:
        self.private = _FakeNetwork("proj_sandbox-edges", labels["network"])
        self.server = _FakeContainer(
            "server", {net: "10.9.0.2" for net in server_networks}, labels["server"]
        )
        self.edges = {
            service: _FakeContainer(
                service,
                {"bridge": "172.17.0.23", **{net: "10.9.0.3" for net in nets}},
                labels["edge"](service),
            )
            for service, nets in edge_networks.items()
        }
        bridge = SimpleNamespace(
            attrs={"IPAM": {"Config": [{"Subnet": subnet} for subnet in bridge_subnets]}}
        )
        self.containers = SimpleNamespace(
            get=lambda _hostname: self.server, list=self._list_containers
        )
        self.networks = SimpleNamespace(
            list=self._list_networks,
            get=lambda name: bridge if name == "bridge" else pytest.fail(name),
        )

    def _list_containers(self, *, filters: dict[str, Any]) -> list[_FakeContainer]:
        assert filters["status"] == "running"
        return [
            edge
            for edge in self.edges.values()
            if _labelled(edge.attrs["Config"]["Labels"], filters["label"])
        ]

    def _list_networks(self, *, filters: dict[str, Any]) -> list[_FakeNetwork]:
        return [self.private] if _labelled(self.private.labels, filters["label"]) else []

    def close(self) -> None:
        pass


def _install_fake_docker(monkeypatch: pytest.MonkeyPatch, client: _FakeDocker) -> None:
    monkeypatch.setitem(sys.modules, "docker", SimpleNamespace(from_env=lambda: client))


def test_sandbox_edges_are_connected_to_the_private_server_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No server port is published on the bridge, so this is the edges' only path."""
    monkeypatch.setenv(onebox.SANDBOX_EDGE_SERVICE_ENV, "sandbox-edge")
    monkeypatch.setenv(onebox.SANDBOX_DNS_EDGE_SERVICE_ENV, "sandbox-dns-edge")
    monkeypatch.setenv(onebox.SANDBOX_EDGE_NETWORK_ENV, "sandbox-edges")
    client = _FakeDocker(
        server_networks=("proj_platform", "proj_sandbox-edges"),
        edge_networks={"sandbox-edge": (), "sandbox-dns-edge": ("proj_sandbox-edges",)},
    )
    _install_fake_docker(monkeypatch, client)

    assert onebox.ensure_sandbox_edge_network() == "proj_sandbox-edges"
    assert client.private.connected == ["sandbox-edge"]
    # A server restart finds both edges already attached and changes nothing.
    assert onebox.ensure_sandbox_edge_network() == "proj_sandbox-edges"
    assert client.private.connected == ["sandbox-edge"]


_INSTALLATION = "astrabox.all-in-one.install=abc123"
_ALL_IN_ONE = {
    "server": {},
    "network": {"astrabox.all-in-one.install": "abc123", "astrabox.all-in-one.role": "sandbox-edges"},
    "edge": lambda role: {"astrabox.all-in-one.install": "abc123", "astrabox.all-in-one.role": role},
}


def test_the_all_in_one_edges_take_the_compose_path_under_their_installation_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The image's server has no Compose project; its edges are found by their owner's labels."""
    monkeypatch.setenv(onebox.SANDBOX_EDGE_SERVICE_ENV, "sandbox-edge")
    monkeypatch.setenv(onebox.SANDBOX_DNS_EDGE_SERVICE_ENV, "sandbox-dns-edge")
    monkeypatch.setenv(onebox.SANDBOX_EDGE_NETWORK_ENV, "sandbox-edges")
    client = _FakeDocker(
        server_networks=("proj_sandbox-edges",),
        edge_networks={"sandbox-edge": (), "sandbox-dns-edge": ()},
        labels=_ALL_IN_ONE,
    )
    _install_fake_docker(monkeypatch, client)
    owner = onebox.EdgeOwner(
        labels=(_INSTALLATION,),
        service_key="astrabox.all-in-one.role",
        network_key="astrabox.all-in-one.role",
        description="all-in-one installation abc123",
    )

    assert onebox.ensure_sandbox_edge_network(owner) == "proj_sandbox-edges"
    assert client.private.connected == ["sandbox-edge", "sandbox-dns-edge"]
    assert onebox.ensure_sandbox_edge_wiring(owner) == "172.17.0.23"
    assert os.environ[onebox.MCP_PROXY_BASE_URL_ENV] == "http://172.17.0.23:8000"
    # Compose's own discovery finds nothing here: the server carries no project.
    with pytest.raises(onebox.OneBoxError, match="com.docker.compose.project"):
        onebox.ensure_sandbox_edge_network()


def test_another_installations_edges_are_not_taken_for_this_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.SANDBOX_EDGE_SERVICE_ENV, "sandbox-edge")
    monkeypatch.setenv(onebox.SANDBOX_EDGE_NETWORK_ENV, "sandbox-edges")
    client = _FakeDocker(
        server_networks=("proj_sandbox-edges",),
        edge_networks={"sandbox-edge": ()},
        labels=_ALL_IN_ONE,
    )
    _install_fake_docker(monkeypatch, client)
    other = onebox.EdgeOwner(
        labels=("astrabox.all-in-one.install=def456",),
        service_key="astrabox.all-in-one.role",
        network_key="astrabox.all-in-one.role",
        description="all-in-one installation def456",
    )

    with pytest.raises(onebox.OneBoxError, match="found 0"):
        onebox.ensure_sandbox_edge_network(other)
    assert client.private.connected == []


def test_sandbox_edge_network_is_required_once_an_edge_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.SANDBOX_EDGE_SERVICE_ENV, "sandbox-edge")
    with pytest.raises(onebox.OneBoxError, match=onebox.SANDBOX_EDGE_NETWORK_ENV):
        onebox.ensure_sandbox_edge_network()


def test_sandbox_edge_network_refuses_a_server_that_is_not_on_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.SANDBOX_EDGE_SERVICE_ENV, "sandbox-edge")
    monkeypatch.setenv(onebox.SANDBOX_EDGE_NETWORK_ENV, "sandbox-edges")
    client = _FakeDocker(
        server_networks=("proj_platform",), edge_networks={"sandbox-edge": ()}
    )
    _install_fake_docker(monkeypatch, client)

    with pytest.raises(onebox.OneBoxError, match="not attached"):
        onebox.ensure_sandbox_edge_network()
    assert client.private.connected == []


def test_bridge_isolation_denies_the_whole_bridge_except_the_sandbox_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other bridge address (gateway, sibling sandboxes, DNS edge) is denied."""
    client = _FakeDocker(server_networks=(), edge_networks={})
    _install_fake_docker(monkeypatch, client)

    value = onebox.ensure_sandbox_bridge_isolation("172.17.0.23")

    assert os.environ[onebox.EGRESS_DENY_CIDRS_ENV] == value
    denied = [ipaddress.ip_network(item) for item in value.split(",")]
    assert sum(network.num_addresses for network in denied) == 2**16 - 1
    assert not any(ipaddress.ip_address("172.17.0.23") in net for net in denied)
    for address in ("172.17.0.1", "172.17.0.2", "172.17.0.22", "172.17.0.24", "172.17.255.255"):
        assert any(ipaddress.ip_address(address) in net for net in denied), address


def test_bridge_isolation_keeps_an_explicit_operator_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.EGRESS_DENY_CIDRS_ENV, "10.96.0.0/12")
    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(from_env=lambda: pytest.fail("an explicit list must not inspect Docker")),
    )

    assert onebox.ensure_sandbox_bridge_isolation("172.17.0.23") == "10.96.0.0/12"


def test_bridge_isolation_refuses_a_callback_base_every_sandbox_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A callback base on the bridge gateway would time out every MCP call."""
    monkeypatch.setenv(onebox.MCP_PROXY_BASE_URL_ENV, "http://172.17.0.1:18200")
    _install_fake_docker(monkeypatch, _FakeDocker(server_networks=(), edge_networks={}))

    with pytest.raises(onebox.OneBoxError, match="172.17.0.1"):
        onebox.ensure_sandbox_bridge_isolation("172.17.0.23")


def test_private_gateway_dns_uses_compose_candidate_only_when_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.GATEWAY_DNS_ADDRESS_ENV, "172.17.0.23")
    monkeypatch.setenv(
        onebox.EGRESS_DNS_UPSTREAM_DEFAULT_ENV,
        "172.17.0.1:1053",
    )

    onebox.ensure_gateway_dns_wiring()

    assert os.environ[onebox.EGRESS_DNS_UPSTREAM_ENV] == "172.17.0.1:1053"


def test_private_gateway_dns_keeps_the_published_bridge_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.GATEWAY_DNS_ADDRESS_ENV, "172.17.0.1")
    monkeypatch.setenv(onebox.EGRESS_DNS_UPSTREAM_ENV, "172.17.0.1:1053")

    assert onebox.ensure_gateway_dns_wiring() == "172.17.0.1"
    assert os.environ[onebox.EGRESS_DNS_UPSTREAM_ENV] == "172.17.0.1:1053"


def test_gateway_dns_refuses_a_non_docker_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.SANDBOX_SERVER_RUNTIME_ENV, "kubernetes")
    with pytest.raises(onebox.OneBoxError, match="external model gateway"):
        onebox.ensure_gateway_dns_wiring()


def test_gateway_dns_process_uses_the_baked_binary_and_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[list[str]] = []

    class _Recorder:
        def __init__(self, name: str, argv: Any, *, prefix_output: bool) -> None:
            assert name == "gateway-dns"
            assert prefix_output is True
            recorded.append(list(argv))

    monkeypatch.setattr(onebox, "_Child", _Recorder)
    monkeypatch.setattr(onebox.os.path, "exists", lambda path: True)
    onebox._spawn_gateway_dns()
    assert recorded == [
        [
            onebox.COREDNS_BIN,
            "-conf",
            onebox.COREDNS_CONFIG_PATH,
            "-dns.port",
            str(onebox.GATEWAY_DNS_PORT),
        ]
    ]


def test_an_external_gateway_disables_the_embedded_one() -> None:
    # (fixture already set ASTRABOX_LITELLM_BASE_URL)
    assert onebox.needs_litellm_gateway() is False


def test_a_plugin_provider_disables_the_embedded_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTRABOX_LITELLM_BASE_URL", raising=False)
    monkeypatch.setenv(onebox.MODEL_PROVIDER_ENV, "acme-gateway")
    assert onebox.needs_litellm_gateway() is False


def test_embedded_gateway_matches_the_declared_model_seam_default() -> None:
    from astrabox.common.utils.settings import AstraBoxRuntimeSettings
    from astrabox.providers import load_entry_point_providers
    from astrabox.seams.model import default_model_endpoint_name

    # The default is declared by the provider that registers at startup, from
    # its `astrabox.providers.model` entry point. Without loading it here the
    # registry holds whatever earlier tests in this process happened to import.
    load_entry_point_providers()

    assert AstraBoxRuntimeSettings.model_fields["model_endpoint_provider"].default == ""
    assert default_model_endpoint_name() == onebox.LITELLM_PROVIDER


def test_master_key_is_generated_persisted_and_kept_server_side(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(onebox.LITELLM_MASTER_KEY_ENV, raising=False)
    monkeypatch.delenv("ASTRABOX_LITELLM_API_KEY", raising=False)
    key = onebox.ensure_litellm_master_key()
    key_file = tmp_path / onebox.LITELLM_KEY_FILENAME
    assert key_file.read_text().strip() == key
    assert (key_file.stat().st_mode & 0o777) == 0o600
    # Exported for the proxy and server-side management only. Sandboxes derive
    # a separate LiteLLM virtual key after the gateway is healthy.
    assert os.environ[onebox.LITELLM_MASTER_KEY_ENV] == key
    assert "ASTRABOX_LITELLM_API_KEY" not in os.environ
    # Idempotent across restarts: the persisted key is reused, not regenerated.
    monkeypatch.delenv(onebox.LITELLM_MASTER_KEY_ENV, raising=False)
    assert onebox.ensure_litellm_master_key() == key


def test_operator_master_key_wins_and_is_not_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv(onebox.LITELLM_MASTER_KEY_ENV, "sk-operator-owned")
    monkeypatch.setenv("ASTRABOX_LITELLM_API_KEY", "sk-operator-sandbox-facing")
    assert onebox.ensure_litellm_master_key() == "sk-operator-owned"
    assert not (tmp_path / onebox.LITELLM_KEY_FILENAME).exists()
    # A non-empty operator sandbox-facing value stays.
    assert os.environ["ASTRABOX_LITELLM_API_KEY"] == "sk-operator-sandbox-facing"


def test_empty_compose_inference_key_is_not_replaced_by_the_master_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(onebox.LITELLM_MASTER_KEY_ENV, raising=False)
    # Compose interpolation produces this exact shape when the operator leaves
    # the optional override unset: the name exists, but its value is empty.
    monkeypatch.setenv(onebox.LITELLM_API_KEY_ENV_NAME, "")

    key = onebox.ensure_litellm_master_key()

    assert key.startswith("sk-astrabox-")
    assert os.environ[onebox.LITELLM_API_KEY_ENV_NAME] == ""


def test_onebox_env_name_mirrors_match_the_provider_module() -> None:
    """onebox keeps literal copies so the env-registry scanner can resolve its
    reads; this pin is what makes a rename in either place fail loud."""
    from astrabox.providers.channel_gateway import (
        CHANNEL_GATEWAY_BASE_URL_ENV,
        CHANNEL_GATEWAY_HOST_ENV,
        CHANNEL_GATEWAY_MANIFEST_ENV,
        CHANNEL_GATEWAY_PORT_ENV,
        CHANNEL_GATEWAY_TOKEN_ENV,
    )
    from astrabox.providers.model import LITELLM_API_KEY_ENV, LITELLM_BASE_URL_ENV

    assert onebox.LITELLM_BASE_URL_ENV_NAME == LITELLM_BASE_URL_ENV
    assert onebox.LITELLM_API_KEY_ENV_NAME == LITELLM_API_KEY_ENV
    assert onebox.CHANNEL_GATEWAY_BASE_URL_ENV == CHANNEL_GATEWAY_BASE_URL_ENV
    assert onebox.CHANNEL_GATEWAY_TOKEN_ENV == CHANNEL_GATEWAY_TOKEN_ENV
    assert onebox.CHANNEL_GATEWAY_HOST_ENV == CHANNEL_GATEWAY_HOST_ENV
    assert onebox.CHANNEL_GATEWAY_PORT_ENV == CHANNEL_GATEWAY_PORT_ENV
    assert onebox.CHANNEL_GATEWAY_MANIFEST_ENV == CHANNEL_GATEWAY_MANIFEST_ENV


# ── the environment children inherit ──────────────────────────────────────


def test_export_default_treats_an_empty_value_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose renders an unset ``${VAR:-}`` as an empty string, so the key
    exists while the deployment configured nothing. ``os.environ.setdefault``
    keeps that empty value, and a child reads "configured as empty"."""
    monkeypatch.setenv("ASTRABOX_TEST_INHERITED", "")

    onebox.export_default("ASTRABOX_TEST_INHERITED", "derived")

    assert os.environ["ASTRABOX_TEST_INHERITED"] == "derived"


def test_export_default_keeps_a_value_the_operator_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_TEST_INHERITED", "operator")

    onebox.export_default("ASTRABOX_TEST_INHERITED", "derived")

    assert os.environ["ASTRABOX_TEST_INHERITED"] == "operator"


def test_export_default_fills_an_absent_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTRABOX_TEST_INHERITED", raising=False)

    onebox.export_default("ASTRABOX_TEST_INHERITED", "derived")

    assert os.environ["ASTRABOX_TEST_INHERITED"] == "derived"


def test_whitespace_only_is_also_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_TEST_INHERITED", "   ")

    onebox.export_default("ASTRABOX_TEST_INHERITED", "derived")

    assert os.environ["ASTRABOX_TEST_INHERITED"] == "derived"


def test_the_session_key_reaches_a_child_when_compose_rendered_it_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The failure this closes: the embedded LiteLLM proxy inherits the empty
    string, its ``read_session_signing_secret`` takes the not-configured
    branch, and that branch imports ``astrabox`` — which is not installed in
    LiteLLM's own virtualenv. The proxy exits, and onebox takes the container
    down with it."""
    monkeypatch.setenv(onebox.AUTH_SESSION_SECRET_ENV, "")
    monkeypatch.setattr(
        "astrabox.identity.session_signing.session_signing_secret",
        lambda: "a-real-key",
    )

    returned = onebox.ensure_shared_identity_key()

    assert returned == "a-real-key"
    assert os.environ[onebox.AUTH_SESSION_SECRET_ENV] == "a-real-key"
