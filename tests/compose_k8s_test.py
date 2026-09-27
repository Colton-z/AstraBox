"""The maintained Kubernetes Compose overlay must replace embedded state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
RESET = object()


@dataclass(frozen=True)
class ComposeOverride:
    value: Any


class ComposeLoader(yaml.SafeLoader):
    pass


def _construct_override(
    loader: yaml.SafeLoader, node: yaml.Node
) -> ComposeOverride:
    if isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        value = loader.construct_scalar(node)
    return ComposeOverride(value)


ComposeLoader.add_constructor("!override", _construct_override)
ComposeLoader.add_constructor("!reset", lambda loader, node: RESET)


def _compose_servers() -> tuple[dict[str, Any], dict[str, Any]]:
    base = yaml.safe_load((ROOT / "containers" / "compose.yaml").read_text())
    overlay = yaml.load(
        (ROOT / "containers" / "compose.kubernetes.yaml").read_text(),
        Loader=ComposeLoader,
    )
    return base["services"]["server"], overlay["services"]["server"]


def test_kubernetes_external_gateway_requires_https() -> None:
    _, overlay = _compose_servers()
    environment = overlay["environment"]

    assert ":?" in environment["ASTRABOX_LITELLM_BASE_URL"]
    assert environment["ASTRABOX_MODEL_GATEWAY_REQUIRE_HTTPS"] == "true"


def test_the_ingress_and_secure_access_settings_reach_the_kubernetes_server() -> None:
    """Compose forwards only the variables a service names.

    A setting the overlay does not name never reaches the lifecycle launcher,
    so an operator's Secure Access switch would leave sandboxes unprotected
    with nothing refusing it.
    """

    base, overlay = _compose_servers()
    environment = {**base["environment"], **overlay["environment"]}
    for name in (
        "ASTRABOX_SANDBOX_SERVER_INGRESS_MODE",
        "ASTRABOX_SANDBOX_SERVER_INGRESS_GATEWAY_ADDRESS",
        "ASTRABOX_SANDBOX_SERVER_INGRESS_ROUTE_MODE",
        "ASTRABOX_SANDBOX_SERVER_INGRESS_SIGNING_KEY",
        "ASTRABOX_SANDBOX_SERVER_INGRESS_SIGNING_KEY_ID",
        "ASTRABOX_SANDBOX_ENDPOINT_SCHEME",
    ):
        assert environment[name] == f"${{{name}:-}}"
    assert (
        environment["ASTRABOX_SANDBOX_SECURE_ACCESS"]
        == "${ASTRABOX_SANDBOX_SECURE_ACCESS:-false}"
    )


def test_kubernetes_external_gateway_drops_embedded_litellm_database_state() -> None:
    base, overlay = _compose_servers()
    embedded_database_environment = {
        "DATABASE_URL",
        "LITELLM_DATABASE_HOST",
        "LITELLM_DATABASE_PASSWORD_FILE",
        "LITELLM_DATABASE_PORT",
    }

    assert embedded_database_environment <= base["environment"].keys()
    assert all(
        overlay["environment"][name] is RESET
        for name in embedded_database_environment
    )
    assert "litellm_database_password" in base["secrets"]

    secrets = overlay["secrets"]
    assert isinstance(secrets, ComposeOverride)
    assert secrets.value == ["astrabox_database_password"]
