#!/usr/bin/env python3
"""Run a second AstraBox server process against a deployment's own database.

A single-container demo stack cannot answer a question about N replicas. The
server boots one uvicorn process with no ``workers=`` argument
(``astrabox/main.py``), so one container holds exactly one of every in-process
structure — one ``_sse_queues``, one runtime registry, one accepted-turn set.
Reproducing anything replica-shaped needs a second process, and it must share
the first one's database, networks and mounts or the comparison is not about
replicas at all.

This clones the running server container from what Docker reports about it
rather than from a compose file, so it stays correct whichever lane deployed
the stack and whatever overlays that lane applied. Everything is copied
verbatim except the published port and the container name.

It is a diagnostic, not a deployment: the clone is disposable, it is labelled
so a later sweep can tell it apart from anything the lane created, and
``--remove`` takes it back out.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys

LABEL = "astrabox.probe.replica-clone"


def _docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        # Creation arguments contain the source's private environment.
        detail = result.stderr.strip() or result.stdout.strip()
        for index, argument in enumerate(args[:-1]):
            if argument == "--env":
                _, _, value = args[index + 1].partition("=")
                if value:
                    detail = detail.replace(value, "<redacted>")
        raise SystemExit(
            f"docker {args[0]} failed ({result.returncode}): {detail}"
        )
    return result.stdout


def _inspect(name: str) -> dict:
    raw = _docker("container", "inspect", name)
    parsed = json.loads(raw)
    if not parsed:
        raise SystemExit(f"no container named {name!r}")
    return parsed[0]


def _clone_arguments(
    source: dict,
    *,
    name: str,
    host_port: int,
    container_port: int,
    bind_ip: str = "127.0.0.1",
) -> tuple[list[str], list[str]]:
    config = source.get("Config") or {}
    args = ["run", "--detach", "--name", name]

    # Compose labels are carried, not dropped. The server finds its sibling
    # services by reading its OWN container's com.docker.compose.project label;
    # without it the process exits 1 on "no com.docker.compose.project label"
    # before it ever listens. A real second replica — what `--scale server=2`
    # would produce — carries these too, so copying them is the faithful clone
    # rather than a trick. Only the container number is re-stamped, because two
    # containers claiming to be number 1 of one service is a state Compose
    # never produces.
    for key, value in (config.get("Labels") or {}).items():
        if key == "com.docker.compose.container-number":
            continue
        args += ["--label", f"{key}={value}"]
    args += ["--label", "com.docker.compose.container-number=2"]
    args += ["--label", f"{LABEL}=1"]

    user = str(config.get("User") or "").strip()
    if user:
        args += ["--user", user]

    # Supplementary groups are not decoration: this deployment joins the host's
    # docker group so the server can reach the Docker socket, and a clone
    # without it starts, logs one line, and exits 1 on
    # `PermissionError(13) ... sandbox-edge`. Carrying --user while dropping
    # --group-add produces a container that looks correctly identified and
    # cannot do the one thing the socket mount was for.
    for group in (source.get("HostConfig") or {}).get("GroupAdd") or []:
        args += ["--group-add", str(group)]

    for variable in config.get("Env") or []:
        # ASTRABOX_PORT would move the listener inside the clone and break the
        # port mapping; every other variable is carried verbatim so the two
        # processes differ in nothing but their address.
        if str(variable).startswith("ASTRABOX_PORT="):
            continue
        args += ["--env", str(variable)]

    for mount in source.get("Mounts") or []:
        kind = mount.get("Type")
        destination = mount.get("Destination")
        if not destination:
            continue
        if kind == "bind":
            source_path = mount.get("Source")
            if not source_path:
                continue
            suffix = "" if mount.get("RW", True) else ":ro"
            args += ["--volume", f"{source_path}:{destination}{suffix}"]
        elif kind == "volume":
            volume_name = mount.get("Name")
            if not volume_name:
                continue
            suffix = "" if mount.get("RW", True) else ":ro"
            args += ["--volume", f"{volume_name}:{destination}{suffix}"]

    for host in (source.get("HostConfig") or {}).get("ExtraHosts") or []:
        args += ["--add-host", str(host)]

    networks = list(((source.get("NetworkSettings") or {}).get("Networks") or {}).keys())
    if not networks:
        raise SystemExit("the source container is on no network; refusing to guess one")
    # docker run attaches one network at creation; the rest are connected after.
    args += ["--network", networks[0]]

    args += ["--publish", f"{bind_ip}:{host_port}:{container_port}"]

    # An entrypoint the deployment overrode is part of how this process starts.
    # Losing it would boot the clone differently from the thing it is supposed
    # to be a second copy of, which is the one difference this must not have.
    # docker run takes a single-string --entrypoint, so a multi-element one has
    # its tail folded onto the command.
    entrypoint = [str(item) for item in (config.get("Entrypoint") or [])]
    command = [str(item) for item in (config.get("Cmd") or [])]
    if entrypoint:
        args += ["--entrypoint", entrypoint[0]]
        command = entrypoint[1:] + command

    args += [str(config.get("Image") or "")]
    args += command
    return args, networks[1:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="running server container name")
    parser.add_argument("--name", default="astrabox-probe-server-b")
    parser.add_argument("--host-port", type=int, default=18099)
    parser.add_argument(
        "--bind-ip", default="127.0.0.1", help="explicit loopback or private IPv4 bind"
    )
    parser.add_argument("--container-port", type=int, default=8000)
    parser.add_argument("--remove", action="store_true", help="remove the clone and exit")
    parser.add_argument("--json-out")
    args = parser.parse_args()
    bind_ip = ipaddress.IPv4Address(args.bind_ip)
    if not (bind_ip.is_loopback or bind_ip.is_private) or bind_ip.is_unspecified:
        raise SystemExit("replica diagnostics require an explicit loopback or private IPv4 bind")
    if not 0 <= args.host_port <= 65535 or not 1 <= args.container_port <= 65535:
        raise SystemExit("invalid replica port")

    if args.remove:
        target = _inspect(args.name)
        source = _inspect(args.source)
        labels = (target.get("Config") or {}).get("Labels") or {}
        source_labels = (source.get("Config") or {}).get("Labels") or {}
        if (
            labels.get(LABEL) != "1"
            or not source_labels.get("com.docker.compose.project")
            or labels.get("com.docker.compose.project")
            != source_labels.get("com.docker.compose.project")
            or target.get("Image") != source.get("Image")
        ):
            raise SystemExit("refusing to remove a container outside the source's replica probe")
        _docker("rm", "-f", target["Id"])
        print(json.dumps({"state": "REMOVED", "name": args.name}, indent=2))
        return 0

    source = _inspect(args.source)
    if not (source.get("State") or {}).get("Running"):
        raise SystemExit(f"{args.source} is not running; clone it from a live process")

    # Let Docker refuse an existing name; never replace another retained probe.
    run_args, extra_networks = _clone_arguments(
        source,
        name=args.name,
        host_port=args.host_port,
        container_port=args.container_port,
        bind_ip=str(bind_ip),
    )
    container_id = _docker(*run_args).strip()
    for network in extra_networks:
        _docker("network", "connect", network, args.name)

    live = _inspect(args.name)
    bindings = live["NetworkSettings"]["Ports"][f"{args.container_port}/tcp"]
    matches = [item for item in bindings if item["HostIp"] == str(bind_ip)]
    if len(matches) != 1:
        raise SystemExit("replica listener does not match its requested bind")

    result = {
        "state": "STARTED",
        "name": args.name,
        "id": container_id,
        "image": (source.get("Config") or {}).get("Image"),
        "origin": f"http://{bind_ip}:{matches[0]['HostPort']}",
        "source_origin_hint": "read the source container's own published port",
        "networks": list(((source.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
    }
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
