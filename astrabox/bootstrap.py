"""Explicit composition root — ``astrabox.bootstrap.bootstrap()``.

Embedders that do not go through the HTTP app (``create_app``) — a worker
process, a script driving the services directly, a host application mounting
AstraBox services inside its own runtime — call this once at startup instead
of relying on import-time side effects:

    from astrabox.bootstrap import bootstrap
    bootstrap()

It performs exactly the provider composition the FastAPI lifespan performs
(the lifespan calls this same function): register the built-in
providers, load every installed entry-point provider distribution, and
publish the deployment-configured default sandbox backend to the seam.
Idempotent — module imports are cached and every registry is
overwrite-by-name, so calling it twice (or after something already imported
a provider module) is safe.

It is also where a setting that the *selected backend* cannot honor becomes a
refusal to start (``BootstrapConfigError``) rather than a knob that silently
does nothing: the backend is only known once composition has resolved it, and
this is the first moment both halves are in hand.

What it deliberately does not do: touch the database (``create_all`` /
migrations), seed Agents or Environments, or start the recovery control plane —
those are service-lifecycle steps that belong to the surface you embed (the
lifespan for the HTTP app; your own startup for a bespoke embedding — see
``docs/embedding.md`` for the full contract and the current process-global
constraints).
"""

from __future__ import annotations

import os

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)

_bootstrapped = False


class BootstrapConfigError(RuntimeError):
    """A configuration combination this deployment cannot honor; the message is
    the operator's answer. Raised during composition so the process refuses to
    start instead of coming up with a setting that does nothing it says."""


def _assert_nas_knobs_are_honored(backend_name: str) -> None:
    """Refuse to boot when the NAS pair is set on a backend that never mounts.

    ``ASTRABOX_NAS_ENABLED`` + ``ASTRABOX_NAS_ENDPOINT`` do two things at once:
    they turn on the runtime NFS mount of the session / conversation storage
    tree, and they move the agent's cwd to ``/root/workspace`` — the directory
    that mount lands on. A backend that declares ``uses_create_oss_mounts``
    performs no runtime mount (every mount step in session bring-up returns
    early on that flag), so on such a backend the pair keeps the cwd move and
    drops the mount: the agent works in a directory nothing mounted, under
    ``/root``, which the in-box workspace API cannot even traverse. That is
    silent breakage rather than an ignored setting, so it is refused here — with
    the backend named — instead of being discovered as an empty workspace
    mid-turn.

    The knobs are not dead code: they are how a mounting backend (any provider
    leaving ``uses_create_oss_mounts`` at its ``False`` default) is pointed at
    its NAS. This check makes them either effective or fatal, never inert.
    """
    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.seams.sandbox import sandbox_for_name

    settings = load_astrabox_settings()
    if not settings.nas_enabled and not str(settings.nas_endpoint or "").strip():
        return
    provider = sandbox_for_name(backend_name)
    if not provider.uses_create_oss_mounts:
        return
    raise BootstrapConfigError(
        f"ASTRABOX_NAS_ENABLED / ASTRABOX_NAS_ENDPOINT are set, but the "
        f"{provider.name!r} sandbox backend mounts its storage at sandbox-create "
        f"time and performs no runtime NAS mount. Honoring them would move the "
        f"agent's working directory to /root/workspace with nothing mounted "
        f"there. Unset both (this backend needs neither), or select a backend "
        f"that performs runtime NAS mounts via ASTRABOX_SANDBOX_BACKEND."
    )


SANDBOX_IDLE_ACTIONS = ("terminate", "pause")


def _assert_idle_action_is_honored(backend_name: str) -> None:
    """Refuse to boot on an idle action this deployment cannot carry out.

    ``ASTRABOX_SANDBOX_IDLE_ACTION=pause`` is a promise about data: it says a
    conversation abandoned past its lease can be picked up again with its
    workspace intact. A backend that cannot snapshot would keep terminating —
    the boxes would still be reclaimed, so nothing would look broken, and the
    loss would only show up as users finding their files gone after a break.
    That is the worst shape a failure can take, so the setting is either
    effective or fatal.

    An unknown value is refused for the ordinary reason: a typo must not be read
    as the default and quietly destroy workspaces the operator meant to keep.
    """
    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.seams.sandbox import sandbox_for_name

    action = str(load_astrabox_settings().sandbox_idle_action or "").strip().lower()
    if action not in SANDBOX_IDLE_ACTIONS:
        raise BootstrapConfigError(
            f"ASTRABOX_SANDBOX_IDLE_ACTION={action!r} is not one of "
            f"{', '.join(repr(a) for a in SANDBOX_IDLE_ACTIONS)}."
        )
    if action != "pause":
        return
    provider = sandbox_for_name(backend_name)
    if provider.supports_pause:
        return
    raise BootstrapConfigError(
        f"ASTRABOX_SANDBOX_IDLE_ACTION=pause, but the {provider.name!r} sandbox "
        f"backend cannot snapshot a sandbox, so an idle box would go on being "
        f"destroyed with its workspace. Set it to 'terminate' to accept that, or "
        f"select a backend that can pause via ASTRABOX_SANDBOX_BACKEND."
    )


def _assert_box_callback_base_is_reachable() -> None:
    """Refuse a box-facing callback URL a sandbox Pod cannot route to.

    ``ASTRABOX_MCP_PROXY_BASE_URL`` is the address the box calls back on for
    platform MCP, lifecycle notices, and the transcript mirror. Left unset
    inside a container it is derived as the server container's own Docker-bridge
    IP, which is correct for the quickstart and wrong for the Kubernetes runtime,
    where a sandbox is a Pod with no route to the bridge at all.

    Getting it wrong does not fail: it makes every turn pay the box's retry budget
    against an address nothing answers. The in-box transcript mirror retries 3×30s
    inside the first turn, so a one-word answer from a fast model takes ~93 s and
    reads as a slow model rather than as a misconfiguration. This refuses to boot
    instead.

    An explicitly set value is the operator's to get right and is never second-
    guessed; an empty one already fails loud downstream where the URL is built.
    """
    import os

    from astrabox.common.utils.settings import load_astrabox_settings

    runtime = str(os.environ.get("ASTRABOX_SANDBOX_SERVER_RUNTIME") or "").strip().lower()
    if runtime != "kubernetes":
        return
    if str(os.environ.get("ASTRABOX_MCP_PROXY_BASE_URL") or "").strip():
        return
    derived = str(load_astrabox_settings().mcp_proxy_base_url or "").strip()
    if not derived:
        return
    raise BootstrapConfigError(
        f"ASTRABOX_MCP_PROXY_BASE_URL is unset, so the box-facing callback base was "
        f"derived as {derived!r} — this container's own Docker-bridge address. Under "
        f"ASTRABOX_SANDBOX_SERVER_RUNTIME=kubernetes a sandbox is a Pod and has no "
        f"route to that, so platform MCP and transcript callbacks would time out "
        f"inside every turn instead of failing. Set ASTRABOX_MCP_PROXY_BASE_URL to an "
        f"address the Pod network can reach (the node's own IP and this server's "
        f"published port, or a Service that fronts it)."
    )


def _assert_model_gateway_https_requirement() -> None:
    """Ask the selected model provider to validate deployment-only settings."""

    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.seams.model import (
        ModelEndpointConfigurationError,
        model_endpoint_for_name,
    )

    settings = load_astrabox_settings()
    try:
        model_endpoint_for_name(settings.model_endpoint_provider).validate_configuration(
            settings=settings
        )
    except ModelEndpointConfigurationError as exc:
        raise BootstrapConfigError(str(exc)) from exc


def _assert_storage_provider_is_registered() -> None:
    """Refuse to boot on a storage provider name nothing registered.

    Resolution is otherwise first reached when a workspace needs its medium,
    which is long after boot and inside a user's request. A typo there reads as
    "creating an assistant is broken" rather than as the configuration mistake
    it is, and by then the deployment has been up long enough to look healthy.
    """
    from astrabox.seams.storage import storage_provider

    try:
        storage_provider().validate_configuration()
    except (RuntimeError, ValueError) as exc:
        raise BootstrapConfigError(f"ASTRABOX_STORAGE_PROVIDER: {exc}") from exc


def _assert_secret_store_is_configured() -> None:
    """Resolve and validate the selected credential store during startup."""
    from astrabox.seams.secrets import SECRET_STORE_ENV, secret_store_for_name

    try:
        secret_store_for_name(None).validate_configuration()
    except RuntimeError as exc:
        raise BootstrapConfigError(f"{SECRET_STORE_ENV}: {exc}") from exc


def bootstrap(*, sandbox_backend: str | None = None) -> None:
    """Compose the provider registries (idempotent; see module docstring).

    ``sandbox_backend`` overrides the settings-resolved default backend name;
    ``None`` publishes ``get_settings().sandbox_backend`` exactly like the app
    lifespan does.
    """
    global _bootstrapped
    from astrabox.config.settings import get_settings
    from astrabox.core.service.orchestrator.runtime.storage.mergerfs import workspace_router
    from astrabox.providers import (
        load_entry_point_providers,
        register_builtin_providers,
    )
    from astrabox.seams.sandbox import set_default_sandbox_backend
    from astrabox.seams.storage import set_configured_storage_provider

    register_builtin_providers()
    load_entry_point_providers()
    effective_backend = (
        sandbox_backend if sandbox_backend is not None else get_settings().sandbox_backend
    )
    set_default_sandbox_backend(effective_backend)
    # Not derived from the sandbox backend: where a workspace lives is a
    # durability decision, not a runtime one, which is why storage is keyed by
    # its own name.
    set_configured_storage_provider(get_settings().storage_provider)
    _assert_secret_store_is_configured()
    _assert_storage_provider_is_registered()
    try:
        workspace_router.validate_configuration()
    except (RuntimeError, ValueError) as exc:
        raise BootstrapConfigError(f"workspace routing: {exc}") from exc
    _assert_nas_knobs_are_honored(effective_backend)
    _assert_idle_action_is_honored(effective_backend)
    _assert_box_callback_base_is_reachable()
    _assert_model_gateway_https_requirement()
    if not _bootstrapped:
        logger.info("astrabox provider composition bootstrapped")
    _bootstrapped = True
