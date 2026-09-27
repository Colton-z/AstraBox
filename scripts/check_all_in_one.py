#!/usr/bin/env python3
"""Every topology setting Compose gives the server is decided for the all-in-one image.

The all-in-one image (``containers/all-in-one/Dockerfile``) is one fixed
topology. Its entry point refuses the settings that would select another one
(``astrabox/deploy/all_in_one.py``, ``REFUSED``) and derives them itself. The
risk is drift: Compose starts setting a new topology variable in
``containers/compose.yaml``, nobody adds it to the refusal table, and a user
who copies it into ``docker run`` gets a container that silently ignores it or,
worse, half-applies it.

So every variable in Compose's ``server.environment`` must be classified here,
exactly once:

* refused by the image (a row of ``REFUSED``);
* passed through, taking effect as it does in Compose (``PASS_THROUGH`` below).

A new Compose variable fails this check until someone decides which. A
pass-through entry that Compose does not set fails too, so the list stays a
statement about the current file.

The image also creates Compose's two sandbox edges itself
(``EDGE_SPECS``). Their image, configuration files and environment must stay
the ones Compose gives its ``sandbox-edge`` and ``sandbox-dns-edge`` services,
and the names the image gives them and their network must be the names
Compose's server is told, or the two shapes would run different boundaries.

    .venv/bin/python scripts/check_all_in_one.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "containers" / "compose.yaml"

sys.path.insert(0, str(REPO_ROOT))

from astrabox.deploy import all_in_one  # noqa: E402
from astrabox.deploy.all_in_one import REFUSED  # noqa: E402

#: Compose server settings the all-in-one leaves to the user, with the same
#: effect as in Compose: model credentials and routes, observability, title
#: generation, image names and mirrors, signing material, sandbox limits.
PASS_THROUGH = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_COMPATIBLE_BASE_URL",
        "OPENAI_COMPATIBLE_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
        "LITELLM_MASTER_KEY",
        "ASTRABOX_TITLE_MODEL_ENABLED",
        "ASTRABOX_TITLE_MODEL_NAME",
        "ASTRABOX_TITLE_MODEL_REQUEST_TIMEOUT_SECONDS",
        "ASTRABOX_ALLOWED_HOSTS",
        "ASTRABOX_SANDBOX_EGRESS_IMAGE",
        # The lifecycle server refuses every mode but dns+nft (sandbox_server.egress).
        "ASTRABOX_SANDBOX_EGRESS_MODE",
        # The Docker runtime refuses Secure Access (sandbox_server).
        "ASTRABOX_SANDBOX_SECURE_ACCESS",
        "ASTRABOX_SANDBOX_SERVER_PORT_RANGE",
        "ASTRABOX_SECRET_STORE",
        "ASTRABOX_AWS_KMS_KEY_ARN",
        "ASTRABOX_VAULT_MASTER_KEY",
        "ASTRABOX_AUTH_SESSION_SECRET",
        "ASTRABOX_TRANSCRIPT_SIGNING_KEY",
        "ASTRABOX_IMAGE_PREFIX",
        "ASTRABOX_IMAGE_TAG",
        "ASTRABOX_AGENT_IMAGE",
        # Workspace storage settings. The volume itself is refused, and with no
        # volume these behave exactly as in a Compose stack that sets none.
        "ASTRABOX_STORAGE_PROVIDER",
        "ASTRABOX_EFS_FILE_SYSTEM_ID",
        "ASTRABOX_WORKSPACE_MOUNTER_IMAGE",
        "ASTRABOX_WORKSPACE_STORAGE_TOPOLOGY",
        "ASTRABOX_WORKSPACE_MOUNT_ROOT",
    }
)


def _environment(service: dict) -> dict[str, str]:
    """A Compose service's environment as a mapping; a null value is empty."""

    environment = service.get("environment") or {}
    if isinstance(environment, list):
        environment = dict(str(item).partition("=")[::2] for item in environment)
    return {str(key): "" if value is None else str(value) for key, value in environment.items()}


def _edge_problems(services: dict) -> list[str]:
    """The image's edges must be Compose's, and named as Compose's server names them."""

    problems: list[str] = []
    server = _environment(services["server"])
    for variable, expected in (
        ("ASTRABOX_SANDBOX_EDGE_SERVICE", all_in_one.EDGE_ROLE),
        ("ASTRABOX_SANDBOX_DNS_EDGE_SERVICE", all_in_one.DNS_EDGE_ROLE),
        ("ASTRABOX_SANDBOX_EDGE_NETWORK", all_in_one.EDGE_NETWORK_ROLE),
    ):
        if server.get(variable) != expected:
            problems.append(
                f"containers/compose.yaml's server sets {variable}={server.get(variable)!r}, "
                f"but the all-in-one image names it {expected!r}"
            )
    if all_in_one.SERVER_ALIAS not in services:
        problems.append(
            f"containers/compose.yaml has no {all_in_one.SERVER_ALIAS!r} service, the name "
            "both edge configurations forward to and the all-in-one image answers to"
        )
    for spec in all_in_one.EDGE_SPECS:
        service = services.get(spec.role)
        if service is None:
            problems.append(f"containers/compose.yaml has no {spec.role!r} service")
            continue
        if service.get("image") != all_in_one.EDGE_IMAGE:
            problems.append(
                f"containers/compose.yaml runs {spec.role} as {service.get('image')!r}, "
                f"the all-in-one image as {all_in_one.EDGE_IMAGE!r}"
            )
        if service.get("network_mode") != "bridge":
            problems.append(f"containers/compose.yaml's {spec.role} is not on Docker's built-in bridge")
        environment = _environment(service)
        if environment != dict(spec.environment):
            problems.append(
                f"containers/compose.yaml gives {spec.role} the environment {environment}, "
                f"the all-in-one image {dict(spec.environment)}"
            )
        mounts = sorted(
            (
                Path(str(volume).split(":")[0]).name,
                str(volume).split(":")[1],
            )
            for volume in service.get("volumes") or []
        )
        files = sorted((item.source.name, item.target) for item in spec.files)
        if mounts != files:
            problems.append(
                f"containers/compose.yaml mounts {mounts} into {spec.role}, the all-in-one "
                f"image copies {files}"
            )
        for item in spec.files:
            if not (REPO_ROOT / "containers" / "sandbox-edge" / item.source.name).is_file():
                problems.append(
                    f"containers/sandbox-edge/{item.source.name}, which the all-in-one image "
                    f"bakes into {item.source.parent}, does not exist"
                )
    return problems


def main() -> int:
    refused = {row[0] for row in REFUSED}
    problems: list[str] = []

    for name in sorted(refused & PASS_THROUGH):
        problems.append(f"{name} is both refused and passed through")

    services = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]
    compose = list(_environment(services["server"]))
    for name in compose:
        if name not in refused | PASS_THROUGH:
            problems.append(
                f"{name} is set by containers/compose.yaml's server but not "
                "classified for the all-in-one image: refuse it in "
                "astrabox/deploy/all_in_one.py REFUSED, or add it to PASS_THROUGH "
                "in this script if it takes effect there as it does in Compose"
            )
    for name in sorted(PASS_THROUGH - set(compose)):
        problems.append(
            f"{name} is listed as passed through, but containers/compose.yaml's "
            "server does not set it; remove it from PASS_THROUGH"
        )
    problems.extend(_edge_problems(services))

    if problems:
        print("all-in-one topology check failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"all-in-one topology check passed: {len(compose)} Compose server settings "
        f"classified ({len(refused)} refusal rows, {len(PASS_THROUGH)} passed through); "
        f"{len(all_in_one.EDGE_SPECS)} sandbox edges match Compose's"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
