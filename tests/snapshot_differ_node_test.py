"""Snapshot acceleration preserves native routing and restores it on failure."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("snapshot_differ_node", ROOT / "tools/snapshot-differ/node.py")
assert SPEC and SPEC.loader
NODE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NODE)
NATIVE = '''version = 3
root = "/var/lib/containerd"
[plugins."io.containerd.service.v1.diff-service"]
  default = ["walking", "another"] # preserve the other differ
  sync_fs = true
[proxy_plugins.existing]
  type = "snapshot"
  address = "/run/existing.sock"
'''


@pytest.mark.parametrize("source", [NATIVE, NATIVE.replace('["walking", "another"]', '[\n  "walking",\n  "another",\n]'), "version = 2\n"])
def test_config_adds_fast_path_and_preserves_existing_settings(source: str) -> None:
    before = tomllib.loads(source)
    rendered = NODE.render_config(source)
    actual = tomllib.loads(rendered)
    assert actual["plugins"][NODE.DIFF_SERVICE]["default"][0] == "astrabox-overlay"
    assert "walking" in actual["plugins"][NODE.DIFF_SERVICE]["default"]
    if "plugins" in before:
        assert actual["plugins"][NODE.DIFF_SERVICE]["default"][1:] == ["walking", "another"]
        assert actual["plugins"][NODE.DIFF_SERVICE]["sync_fs"] is True
        assert actual["proxy_plugins"]["existing"] == before["proxy_plugins"]["existing"]
        assert actual["root"] == before["root"]
    assert NODE.render_config(rendered) == rendered


@pytest.mark.parametrize("source, refusal", [
    ('version = 1\n', "version"),
    ('version = 3\nimports = ["other.toml"]\n', "imports"),
    (NATIVE.replace('["walking", "another"]', '["another"]'), "walking"),
    (NATIVE + '[proxy_plugins.astrabox-overlay]\ntype="diff"\naddress="/foreign.sock"\n', "different proxy"),
])
def test_config_refuses_ambiguous_or_incompatible_routing(source: str, refusal: str) -> None:
    with pytest.raises(ValueError, match=refusal):
        NODE.render_config(source)


def test_config_preserves_empty_native_import_glob(tmp_path: Path) -> None:
    pattern = str(tmp_path / "conf.d/*.toml")
    source = NATIVE.replace("version = 3", "version = 3\nimports = " + json.dumps([pattern]))
    rendered = NODE.render_config(source)
    assert tomllib.loads(rendered)["imports"] == [pattern]
    assert NODE.render_config(rendered) == rendered
    (tmp_path / "conf.d").mkdir()
    (tmp_path / "conf.d/override.toml").write_text('version = 3\n')
    with pytest.raises(ValueError, match="active imported files"):
        NODE.render_config(source)


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    for key, name in (("BINARY", "binary"), ("UNIT", "unit"), ("CONFIG", "config.toml"), ("STATE", "state")):
        monkeypatch.setattr(NODE, key, tmp_path / name)
    NODE.CONFIG.write_text(NATIVE)
    state = {"active": False, "enabled": False, "events": [], "fail_restart": False, "fail_health": False}
    monkeypatch.setattr(NODE.os, "geteuid", lambda: 0)

    def command(args: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        state["events"].append((tuple(args), NODE.CONFIG.read_text()))
        rc = 0
        out = ""
        if args[:2] == ["systemctl", "is-active"]:
            rc = 0 if state["active"] else 3
        elif args[:2] == ["systemctl", "is-enabled"]:
            rc = 0 if state["enabled"] else 1
        elif args[:2] == ["systemctl", "enable"]:
            state["enabled"] = True
        elif args[:2] == ["systemctl", "disable"]:
            state["enabled"] = False
        elif args[:2] == ["systemctl", "stop"]:
            state["active"] = False
        elif args == ["systemctl", "restart", NODE.SERVICE]:
            state["active"] = True
        elif args == ["systemctl", "restart", "containerd"] and state["fail_restart"]:
            state["fail_restart"] = False
            raise RuntimeError("injected containerd restart failure")
        elif args[0] == "containerd":
            tomllib.loads(Path(args[2]).read_text())
        elif args[0] == "ctr":
            out = "io.containerd.differ.v1 astrabox-overlay linux/amd64 ok\nio.containerd.differ.v1 walking linux/amd64 ok\n"
        return subprocess.CompletedProcess(args, rc, out, "")

    def healthy(binary_sha: str) -> None:
        state["events"].append((("health",), NODE.CONFIG.read_text()))
        if state["fail_health"]:
            raise RuntimeError("injected plugin startup failure")
        assert state["active"] and NODE.digest(NODE.BINARY.read_bytes()) == binary_sha

    monkeypatch.setattr(NODE, "command", command)
    monkeypatch.setattr(NODE, "healthy", healthy)
    monkeypatch.setattr(NODE, "active_binary", lambda value: state["active"] and NODE.BINARY.exists() and NODE.digest(NODE.BINARY.read_bytes()) == value)
    return state


def inputs() -> tuple[bytes, bytes, dict]:
    binary, unit = b"verified binary", b"verified service"
    return binary, unit, {"source_sha256": "a" * 64, "binary_sha256": NODE.digest(binary), "unit_sha256": NODE.digest(unit)}


def test_install_starts_plugin_before_routing_and_is_idempotent(machine: dict) -> None:
    first = NODE.install(*inputs())
    assert first["state"] == "CONFIGURED" and first["changed"] is True
    events = machine["events"]
    health = next(i for i, event in enumerate(events) if event[0] == ("health",))
    restart = next(i for i, event in enumerate(events) if event[0] == ("systemctl", "restart", "containerd"))
    assert health < restart
    assert events[health][1] == NATIVE
    machine["events"].clear()
    second = NODE.install(*inputs())
    assert second["changed"] is False
    assert not any("restart" in event[0] for event in machine["events"])
    assert len(list(NODE.STATE.glob("backup-*"))) == 1
    assert json.loads((NODE.STATE / "receipt.json").read_text())["binary_sha256"] == inputs()[2]["binary_sha256"]


@pytest.mark.parametrize("existing", [False, True])
def test_private_install_umask_does_not_hide_system_executables(machine: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, existing: bool) -> None:
    directory = tmp_path / "libexec"
    if existing:
        directory.mkdir(mode=0o700)
    monkeypatch.setattr(NODE, "BINARY", directory / "astrabox-snapshot-differ")
    previous = os.umask(0o077)
    try:
        NODE.install(*inputs())
    finally:
        os.umask(previous)
    assert directory.stat().st_mode & 0o777 == 0o755
    assert NODE.BINARY.stat().st_mode & 0o777 == 0o755
    assert NODE.STATE.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o700 for p in NODE.STATE.glob("backup-*"))


@pytest.mark.parametrize("fail_restart", [False, True])
def test_config_permissions_survive_install_and_rollback(machine: dict, fail_restart: bool) -> None:
    NODE.CONFIG.chmod(0o600)
    machine["fail_restart"] = fail_restart
    if fail_restart:
        with pytest.raises(RuntimeError, match="restart failure"):
            NODE.install(*inputs())
        assert NODE.CONFIG.read_text() == NATIVE
    else:
        NODE.install(*inputs())
    assert NODE.CONFIG.stat().st_mode & 0o777 == 0o600


def test_bad_plugin_cannot_change_containerd_routing(machine: dict) -> None:
    machine["fail_health"] = True
    with pytest.raises(RuntimeError, match="startup failure"):
        NODE.install(*inputs())
    assert NODE.CONFIG.read_text() == NATIVE
    assert not NODE.BINARY.exists() and not NODE.UNIT.exists()
    assert machine["active"] is False and machine["enabled"] is False
    assert not any(event[0] == ("systemctl", "restart", "containerd") for event in machine["events"])


def test_new_import_during_startup_prevents_routing_change(machine: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    imports = tmp_path / "conf.d"
    imports.mkdir()
    source = NATIVE.replace("version = 3", "version = 3\nimports = " + json.dumps([str(imports / "*.toml")]))
    NODE.CONFIG.write_text(source)
    check_health = NODE.healthy

    def healthy(binary_sha: str) -> None:
        check_health(binary_sha)
        (imports / "override.toml").write_text('version = 3\n')

    monkeypatch.setattr(NODE, "healthy", healthy)
    with pytest.raises(ValueError, match="active imported files"):
        NODE.install(*inputs())
    assert NODE.CONFIG.read_text() == source
    assert not NODE.BINARY.exists() and not NODE.UNIT.exists()
    assert machine["active"] is False
    assert not any(event[0] == ("systemctl", "restart", "containerd") for event in machine["events"])


def test_failed_reload_restores_native_routing_before_plugin_shutdown(machine: dict) -> None:
    machine["fail_restart"] = True
    with pytest.raises(RuntimeError, match="restart failure"):
        NODE.install(*inputs())
    events = machine["events"]
    restarts = [i for i, event in enumerate(events) if event[0] == ("systemctl", "restart", "containerd")]
    stop = next(i for i, event in enumerate(events) if event[0] == ("systemctl", "stop", NODE.SERVICE))
    assert len(restarts) == 2 and restarts[-1] < stop
    assert events[restarts[-1]][1] == NATIVE
    assert NODE.CONFIG.read_text() == NATIVE


def test_mismatched_binary_is_rejected_before_any_mutation(machine: dict) -> None:
    binary, unit, identity = inputs()
    with pytest.raises(ValueError, match="hashes differ"):
        NODE.install(binary + b"foreign", unit, identity)
    assert not machine["events"]
    assert NODE.CONFIG.read_text() == NATIVE
