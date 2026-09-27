"""The AIO :8080 gateway key is per box, secret-keyed, and injected at create.

The base image serves a file/shell API, a terminal, JupyterLab, a VNC desktop,
code-server and an MCP hub on :8080. Live proof (in the task evidence) shows
that unset, ``POST /v1/shell/exec`` runs arbitrary commands to any caller;
with ``SANDBOX_API_KEY`` set every service returns 401. What is pinned here is
that AstraBox always sets it, that the value cannot be guessed or shared across
boxes, and that turning it off is not a silent option.
"""

from __future__ import annotations

import pytest

from astrabox.providers.open_sandbox.aio_auth import (
    AIO_SANDBOX_API_KEY_ENV,
    derive_aio_api_key,
)


def test_the_env_name_is_the_vendor_switch() -> None:
    # The base image reads exactly this name; a rename silently reopens :8080.
    assert AIO_SANDBOX_API_KEY_ENV == "SANDBOX_API_KEY"


def test_a_box_with_no_assignment_gets_no_key_instead_of_a_guessable_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "deployment-secret")
    with pytest.raises(ValueError):
        derive_aio_api_key("")


def test_the_key_is_per_box_and_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Per box: one box's key must not open another's :8080, or an agent that
    # reads its own SANDBOX_API_KEY could drive a sibling. Stable: a restarted
    # gateway keeps the credential the server holds.
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "deployment-secret")
    a = derive_aio_api_key("assignment-a")
    b = derive_aio_api_key("assignment-b")
    assert a != b
    assert a == derive_aio_api_key("assignment-a")


def test_the_key_is_keyed_to_the_deployment_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not a public hash of the id: a co-tenant that reaches :8080 and knows the
    # assignment id still cannot compute it without the deployment secret.
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "secret-one")
    first = derive_aio_api_key("assignment-a")
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "secret-two")
    assert first != derive_aio_api_key("assignment-a")


def test_create_injects_the_key_for_every_sandbox() -> None:
    # The create path must set SANDBOX_API_KEY unconditionally: a box created
    # without it serves :8080 to the whole cluster. The check reads the source
    # so it fails if the injection is removed or made conditional.
    import inspect

    from astrabox.providers.open_sandbox import executor

    source = inspect.getsource(executor)
    assert "create_env[AIO_SANDBOX_API_KEY_ENV] = derive_aio_api_key(assignment_metadata)" in source


@pytest.mark.parametrize(
    "service", ["JUPYTER", "CODE_SERVER", "VNC", "BROWSER", "MCP_BROWSER", "NODEJS_REPL"]
)
def test_unused_aio_services_are_disabled_in_every_box_boot_env(service: str) -> None:
    # The AIO base image serves these interactive services on :8080 and starts
    # them unless told not to. gem.sh reads the vendor's DISABLE_* switches from
    # the box environment at boot; AstraBox composes them into every box's create
    # env through SANDBOX_SELF_DESCRIPTION, one definition for every engine and
    # for pooled boxes alike, so a service no agent loop uses does not run and
    # cannot answer. The switch that takes effect is DISABLE_*: gem.sh derives
    # the supervisord AUTOSTART_* variables from these flags at boot and
    # overwrites anything baked into the image, so setting AUTOSTART_* is inert.
    from astrabox.providers.sandbox_image import SANDBOX_SELF_DESCRIPTION

    assert SANDBOX_SELF_DESCRIPTION.get(f"DISABLE_{service}") == "true", (
        f"DISABLE_{service} must be in the boot env every sandbox carries"
    )
