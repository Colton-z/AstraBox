"""Run the candidate with a separately installed skill supplier and SSH fixture.

The isolated sshd uses its own port, host key and authorized key file. The
candidate's original container is stopped, retained and restored after the
journey. No model calls or business checks run during fixture setup.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import shlex
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from add_server_replica import _clone_arguments, _docker, _inspect

LABEL = "astrabox.probe.installed-skills"


def _run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"skill fixture command {args[0]} failed: {result.stderr[-2000:]}")
    return result.stdout.strip()


def start(source_name: str, origin: str, root: Path) -> dict:
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = _inspect(source_name)
    if not source["State"]["Running"]:
        raise RuntimeError("the candidate server must be running before fixture setup")
    server_group = _run("docker", "exec", source_name, "id", "-g")
    source_env = dict(item.split("=", 1) for item in source["Config"].get("Env", []) if "=" in item)
    base_ref = source["Config"]["Image"]
    if json.loads(_docker("image", "inspect", base_ref))[0]["Id"] != source["Image"]:
        raise RuntimeError("the candidate image tag does not name the running image")
    name = f"astrabox-skills-{secrets.token_hex(5)}"
    package = Path(__file__).with_name("installed-skills")
    digest = hashlib.sha256(source["Image"].encode())
    for path in sorted(package.rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    image = "astrabox-e2e-installed-skills:" + digest.hexdigest()[:20]
    _run("docker", "build", "--pull=false", "--tag", image,
         "--build-arg", f"BASE_IMAGE={base_ref}",
         "--build-arg", "RUNTIME_USER=" + (source["Config"].get("User") or "0"), str(package))
    installed = json.loads(_docker("image", "inspect", image))[0]
    base = json.loads(_docker("image", "inspect", source["Image"]))[0]
    assert installed["RootFS"]["Layers"][:len(base["RootFS"]["Layers"])] == base["RootFS"]["Layers"]

    store = root / "store"
    store.mkdir(mode=0o700)
    versions = {}
    for number in (1, 2):
        text = f"---\nname: native-marker\ndescription: Print the supplier marker on request.\n---\nNATIVE-SKILL-VERSION-{number}-{name}\n"
        path = store / f"version-{number}.md"
        path.write_text(text)
        versions[hashlib.sha256(text.encode()).hexdigest()] = path.name
    (store / "versions.json").write_text(json.dumps(versions))
    (store / "access-key").write_text(secrets.token_hex(32))
    (store / "access-key").chmod(0o600)
    (store / "fetches.jsonl").touch()
    # The host fixture owns the source; the candidate's existing group can
    # read its credential and append acquisition facts without running as root.
    _run("sudo", "chgrp", "-R", server_group, str(store))
    store.chmod(0o750)
    for path in store.iterdir():
        path.chmod(0o640)
    (store / "fetches.jsonl").chmod(0o660)
    repo = root / "repository"
    _run("git", "init", str(repo))
    skill = repo / "skills" / "native-marker"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_bytes((store / "version-1.md").read_bytes())
    (skill / "scripts" / "marker").write_text("#!/bin/sh\nprintf 'native-skill-script\\n'\n")
    (skill / "scripts" / "marker").chmod(0o755)
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "-c", "user.name=Skill Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Seed native skill content")
    revision = _run("git", "-C", str(repo), "rev-parse", "HEAD")
    bare = root / "skills.git"
    _run("git", "clone", "--bare", str(repo), str(bare))
    for key in ("host", "deploy", "wrong"):
        _run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / key))
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        port = listener.getsockname()[1]
    address = urlparse(origin)
    route = json.loads(_run("ip", "-json", "route", "get", "1.1.1.1"))[0]
    host = route["prefsrc"]
    user = _run("id", "-un")
    wrapper = root / "upload-pack"
    normal_command = f"git-upload-pack '{bare}'"
    slow_command = f"git-upload-pack '{root / 'slow.git'}'"
    wrapper.write_text(f'''#!/bin/sh
set -eu
case "$SSH_ORIGINAL_COMMAND" in
  {shlex.quote(normal_command)}) ;;
  {shlex.quote(slow_command)})
    if test -f {shlex.quote(str(store / 'slow-resolved'))}; then
      touch {shlex.quote(str(store / 'slow-fetch-started'))}
      sleep 4
    else
      touch {shlex.quote(str(store / 'slow-resolved'))}
    fi
    ;;
  *) exit 126 ;;
esac
exec /usr/bin/git-upload-pack {shlex.quote(str(bare))}
''')
    wrapper.chmod(0o700)
    config = root / "sshd_config"
    config.write_text(f"""Port {port}
ListenAddress {host}
HostKey {root / 'host'}
PidFile {root / 'sshd.pid'}
AuthorizedKeysFile {root / 'deploy.pub'}
AllowUsers {user}
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
UsePAM yes
PermitRootLogin no
DisableForwarding yes
PermitTTY no
ForceCommand {wrapper}
LogLevel VERBOSE
""")
    fields = (root / "host.pub").read_text().split()
    known_hosts = f"[{host}]:{port} {fields[0]} {fields[1]}"
    approved = source_env.get("ASTRABOX_DEPLOY_KEY_SECRET_NAMES", "")
    extra_names = "fixture-skill-deploy,fixture-skill-wrong,fixture-skill-missing"
    env_file = root / "server.env"
    lines = ["ASTRABOX_DEPLOY_KEY_SECRET_NAMES=" + ",".join(filter(None, (approved, extra_names)))]
    for env_name, key in (("FIXTURE_SKILL_DEPLOY", "deploy"), ("FIXTURE_SKILL_WRONG", "wrong")):
        lines.append(env_name + "=" + (root / key).read_text().replace("\n", "\\n"))
    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)
    ports = [(p, bindings) for p, bindings in source["NetworkSettings"]["Ports"].items()
             if bindings and any(int(b["HostPort"]) == address.port for b in bindings)]
    if len(ports) != 1 or address.scheme != "http":
        raise RuntimeError("the fixture requires the candidate's private HTTP listener")
    container_port, bindings = ports[0]
    clone_env = [v for v in source["Config"].get("Env", []) if not v.startswith("ASTRABOX_DEPLOY_KEY_SECRET_NAMES=")]
    clone = {**source, "Config": {**source["Config"], "Image": image, "Env": clone_env}}
    args, networks = _clone_arguments(clone, name=name, host_port=int(address.port),
                                      container_port=int(container_port.split("/")[0]), bind_ip=str(address.hostname))
    extra = ["--label", f"{LABEL}={source['Id']}", "--env-file", str(env_file),
             "--volume", f"{store}:/opt/astrabox-skill-fixture"]
    for binding in bindings:
        if binding["HostIp"] != address.hostname:
            extra += ["--publish", f"{binding['HostIp']}:{binding['HostPort']}:{container_port}"]
    args[1:1] = extra
    receipt = {"source": source_name, "source_id": source["Id"], "name": name,
               "image_id": installed["Id"], "root": str(root), "host": host, "origin": origin,
               "ssh_source": f"ssh://{user}@{host}:{port}{bare}", "revision": revision,
               "known_hosts": known_hosts, "versions": list(versions), "state": "PREPARED"}
    (root / "receipt.json").write_text(json.dumps(receipt, indent=2))
    try:
        _run("sudo", "/usr/sbin/sshd", "-f", str(config), "-E", str(root / "sshd.log"))
        _docker("stop", "--time", "10", source_name)
        _docker(*args)
        for network in networks:
            _docker("network", "connect", network, name)
        receipt["state"] = "STARTED"
        (root / "receipt.json").write_text(json.dumps(receipt, indent=2))
        return receipt
    except BaseException:
        restore(receipt)
        raise


def restore(receipt: dict) -> None:
    root = Path(receipt["root"])
    try:
        found = _docker("ps", "-aq", "--filter", f"name=^/{receipt['name']}$").strip()
        if found:
            clone = _inspect(receipt["name"])
            if clone["Image"] != receipt["image_id"] or clone["Config"]["Labels"].get(LABEL) != receipt["source_id"]:
                raise RuntimeError("refusing to remove a container outside the skill fixture")
            _docker("rm", "-f", receipt["name"])
    finally:
        source = _inspect(receipt["source"])
        if source["Id"] != receipt["source_id"]:
            raise RuntimeError("the original candidate server identity changed")
        _docker("start", receipt["source"])
        pid_path = root / "sshd.pid"
        if pid_path.exists():
            pid = int(pid_path.read_text())
            command = Path(f"/proc/{pid}/cmdline").read_bytes().decode().replace("\0", " ")
            if str(root / "sshd_config") not in command:
                raise RuntimeError("refusing to stop an sshd outside the skill fixture")
            _run("sudo", "kill", str(pid))
        for name in ("deploy", "wrong", "host", "server.env", "store/access-key"):
            (root / name).unlink(missing_ok=True)
        deadline = time.monotonic() + 60
        while True:
            try:
                with urlopen(receipt["origin"] + "/healthz", timeout=2) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("the restored candidate server did not become healthy")
            time.sleep(0.5)
        receipt["state"] = "RESTORED"
        (root / "receipt.json").write_text(json.dumps(receipt, indent=2))
