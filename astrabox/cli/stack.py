"""``astrabox up``/``down``/``status``/``logs`` — the local Compose deployment.

These wrap the maintained entry point ``scripts/compose.sh`` rather than
re-deriving what it does. That script creates the persistent database
credentials, refuses to start against an orphaned PostgreSQL volume, resolves
the Docker socket's group, and names the Compose project — every one of which a
second implementation here would eventually get wrong.

What these commands add on top is the part a caller cannot script reliably:
the preflight that says *why* a start will not work, and a bounded wait that
ends with a definite verdict instead of a guess about when the deployment
became usable.

``up``, ``down`` and ``logs`` need a repository checkout, because the Compose
stack's definition lives there. ``status`` needs only an address, so it works
against any deployment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from astrabox.cli.client import resolve_endpoint
from astrabox.cli.flags import connection_flags
from astrabox.cli.output import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_UNREACHABLE,
    EXIT_USAGE,
    FORMAT_TABLE,
    CliError,
    emit,
)

#: Files that together identify a checkout able to start the stack.
_STACK_MARKERS = ("scripts/compose.sh", "containers/compose.yaml")

#: How long `up` waits for the deployment to answer before giving up. A first
#: start pulls images and initialises PostgreSQL, so this is minutes, not
#: seconds; `--wait-seconds` moves it and `--no-wait` skips the wait entirely.
DEFAULT_WAIT_SECONDS = 300

#: Gap between readiness probes while waiting.
_PROBE_INTERVAL_SECONDS = 2.0

#: Per-probe deadline. A deployment mid-start refuses the connection outright;
#: this bounds the case where it accepts and then does not answer.
_PROBE_TIMEOUT_SECONDS = 5.0


def register(subparsers: Any) -> None:
    """Add the local-deployment subcommands to the top-level parser."""
    common = connection_flags()

    up = subparsers.add_parser(
        "up",
        parents=[common],
        help="Start the maintained local deployment and wait for it to answer.",
        description=(
            "Run scripts/compose.sh up -d from the repository checkout, then "
            "poll the deployment until it is ready. Reports the address to "
            "open and anything the preflight found missing."
        ),
    )
    up.add_argument("--build", action="store_true", help="Rebuild images before starting.")
    up.add_argument(
        "--wait-seconds",
        type=int,
        default=DEFAULT_WAIT_SECONDS,
        help="How long to wait for readiness (default: %(default)s).",
    )
    up.add_argument(
        "--no-wait",
        action="store_true",
        help="Return once Compose has started the services, without waiting.",
    )
    up.set_defaults(func=_cmd_up)

    down = subparsers.add_parser(
        "down",
        parents=[common],
        help="Stop the maintained local deployment.",
        description="Run scripts/compose.sh down from the repository checkout.",
    )
    down.add_argument(
        "--volumes",
        action="store_true",
        help="Also remove the named volumes. This deletes the database and all state.",
    )
    down.set_defaults(func=_cmd_down)

    status = subparsers.add_parser(
        "status",
        parents=[common],
        help="Report whether a deployment is up and serving.",
        description=(
            "Probe /healthz and /readyz. Works against any deployment, local or "
            "remote; no checkout needed."
        ),
    )
    status.set_defaults(func=_cmd_status)

    logs = subparsers.add_parser(
        "logs",
        parents=[common],
        help="Show logs from the local deployment's services.",
        description="Run scripts/compose.sh logs from the repository checkout.",
    )
    logs.add_argument(
        "service",
        nargs="?",
        help="One service (server, postgres, redis, sandbox-edge, sandbox-dns-edge).",
    )
    logs.add_argument("--tail", default="200", help="Lines per service (default: %(default)s).")
    logs.add_argument("-f", "--follow", action="store_true", help="Stream new lines.")
    logs.set_defaults(func=_cmd_logs)


def _cmd_up(args: Any) -> int:
    """Handle ``astrabox up``."""
    root = _repository_root()
    docker_version = _docker_server_version()
    image = _agent_image_status()
    endpoint = resolve_endpoint(endpoint=args.endpoint, token=args.token)

    command = ["up", "-d"]
    if args.build:
        command.append("--build")
    _compose(root, command, output=args.output)

    ready = None
    if not args.no_wait:
        ready = _wait_until_ready(endpoint.base_url, deadline_seconds=args.wait_seconds)

    payload = {
        "endpoint": endpoint.base_url,
        "repository": str(root),
        "docker_server_version": docker_version,
        "agent_image": image,
        "ready": ready,
    }
    note = None
    if args.output == FORMAT_TABLE:
        note = f"AstraBox is at {endpoint.base_url}"
        if not image["present"]:
            note += (
                f"\nThe agent sandbox image {image['name']} is not on this host. "
                "Sessions on an Environment that uses it will fail to start; "
                "build it with `make build-agent-image`."
            )
    emit(payload, output=args.output, note=note)
    return EXIT_OK


def _cmd_down(args: Any) -> int:
    """Handle ``astrabox down``."""
    root = _repository_root()
    command = ["down"]
    if args.volumes:
        command.append("--volumes")
    _compose(root, command, output=args.output)
    emit(
        {"repository": str(root), "volumes_removed": bool(args.volumes)},
        output=args.output,
        note="stopped" if args.output == FORMAT_TABLE else None,
    )
    return EXIT_OK


def _cmd_status(args: Any) -> int:
    """Handle ``astrabox status``."""
    endpoint = resolve_endpoint(endpoint=args.endpoint, token=args.token)
    healthy, health_detail = probe(endpoint.base_url, "/healthz")
    ready, ready_detail = probe(endpoint.base_url, "/readyz")
    payload = {
        "endpoint": endpoint.base_url,
        "healthy": healthy,
        "ready": ready,
        "detail": health_detail if not healthy else ready_detail,
    }

    if not healthy:
        raise CliError(
            f"no deployment answering at {endpoint.base_url}: {health_detail}",
            exit_code=EXIT_UNREACHABLE,
            details=payload,
        )
    emit(payload, output=args.output)
    return EXIT_OK


def _cmd_logs(args: Any) -> int:
    """Handle ``astrabox logs``."""
    root = _repository_root()
    command = ["logs", "--tail", str(args.tail)]
    if args.follow:
        command.append("--follow")
    if args.service:
        command.append(args.service)
    # Logs are the payload here, so they go to this process's stdout in both
    # formats; there is no JSON result to keep clean.
    return _compose(root, command, output=FORMAT_TABLE)


def _repository_root() -> Path:
    """The checkout that carries the Compose stack, searching upward from cwd.

    The stack's definition, its entry point and the Caddy/CoreDNS material it
    mounts all live in the repository, so there is nothing to start without
    one. Failing with the marker files named is more useful than a Compose
    error about a missing file.
    """
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if all((candidate / marker).exists() for marker in _STACK_MARKERS):
            return candidate
    raise CliError(
        "no AstraBox checkout here: this command starts the maintained Compose "
        "stack, whose definition lives in the repository",
        exit_code=EXIT_USAGE,
        details={"searched_from": str(Path.cwd()), "expected": list(_STACK_MARKERS)},
    )


def _docker_server_version() -> str:
    """The Docker daemon's version, or a loud failure naming what is wrong.

    Separating "docker is not installed" from "the daemon is not answering"
    matters because the fixes differ, and Compose reports both as a generic
    connection error.
    """
    if shutil.which("docker") is None:
        raise CliError(
            "docker is not on PATH; the local deployment runs on Docker",
            exit_code=EXIT_USAGE,
        )
    probe = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise CliError(
            "the Docker daemon is not answering: " + (probe.stderr.strip() or "no detail"),
            exit_code=EXIT_FAILED,
        )
    return probe.stdout.strip()


def _agent_image_status() -> dict[str, Any]:
    """Whether the configured agent sandbox image is on this host.

    Reported rather than enforced: an Environment may pin an image of its own,
    or pull one from a registry, so a missing local image is not always a
    misconfiguration. It is, however, the usual reason a deployment starts
    cleanly and then fails to open its first session.
    """
    name = os.environ.get("ASTRABOX_AGENT_IMAGE") or "astrabox/sandbox-claude-code:latest"
    probe = subprocess.run(
        ["docker", "image", "inspect", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return {"name": name, "present": probe.returncode == 0}


def _compose(root: Path, command: list[str], *, output: str) -> int:
    """Run ``scripts/compose.sh`` with the given arguments.

    Compose's own output is inherited in the table format so a long build stays
    visible, and redirected to stderr under ``--output json`` so stdout carries
    only the result object.
    """
    script = root / "scripts" / "compose.sh"
    stream = None if output == FORMAT_TABLE else sys.stderr
    completed = subprocess.run(
        [str(script), *command],
        cwd=str(root),
        stdout=stream,
        check=False,
    )
    if completed.returncode != 0:
        raise CliError(
            f"scripts/compose.sh {' '.join(command)} exited {completed.returncode}",
            exit_code=EXIT_FAILED,
            details={"repository": str(root)},
        )
    return EXIT_OK


def _wait_until_ready(base_url: str, *, deadline_seconds: int) -> bool:
    """Poll ``/readyz`` until the deployment answers, or fail with what it said.

    A start that returns without a verdict is the failure this prevents: a
    caller cannot tell a deployment still initialising its database from one
    that died thirty seconds in, and both look like an empty console.
    """
    deadline = time.monotonic() + max(deadline_seconds, 0)
    last_detail = "no response yet"
    while time.monotonic() < deadline:
        answered, last_detail = probe(base_url, "/readyz")
        if answered:
            return True
        time.sleep(_PROBE_INTERVAL_SECONDS)
    raise CliError(
        f"deployment at {base_url} was not ready within {deadline_seconds}s: {last_detail}",
        exit_code=EXIT_UNREACHABLE,
        details={"endpoint": base_url, "waited_seconds": deadline_seconds},
    )


def probe(base_url: str, path: str) -> tuple[bool, str]:
    """Whether the deployment answers 2xx at this path, and what it said.

    The detail travels with the verdict so a timeout can report what it kept
    seeing — a refused connection and a 503 mean different things — rather than
    only that time ran out.
    """
    import httpx

    try:
        response = httpx.get(f"{base_url}{path}", timeout=_PROBE_TIMEOUT_SECONDS)
    except httpx.RequestError as exc:
        return False, str(exc)
    return response.status_code < 300, f"HTTP {response.status_code}"


__all__ = ["DEFAULT_WAIT_SECONDS", "probe", "register"]
