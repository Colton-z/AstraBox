"""Explicit composition root — ``astrabox.bootstrap.bootstrap()``.

Embedders that do not go through the HTTP app (``create_app``) — a worker
process, a script driving the services directly, a host application mounting
AstraBox services inside its own runtime — call this once at startup instead
of relying on import-time side effects:

    from astrabox.bootstrap import bootstrap
    bootstrap()

It performs exactly the provider composition the FastAPI lifespan performs
(the lifespan calls this same function): register the built-in
providers, load every installed entry-point provider distribution,
publish the deployment-configured default sandbox backend and the set of
enabled sandbox backends to the seam, and select the scheduler provider.
Idempotent — module imports are cached and every registry is
overwrite-by-name, so calling it twice (or after something already imported
a provider module) is safe.

It is also where a setting that the *selected backend* cannot honor becomes a
refusal to start (``BootstrapConfigError``) rather than a knob that silently
does nothing: the backend is only known once composition has resolved it, and
this is the first moment both halves are in hand.

What it deliberately does not do: touch the database (``create_all`` /
migrations), seed Agents or Environments, start the scheduler provider, or
start the recovery control plane —
those are service-lifecycle steps that belong to the surface you embed (the
lifespan for the HTTP app; your own startup for a bespoke embedding — see
``docs/embedding.md`` for the full contract and the current process-global
constraints).
"""

from __future__ import annotations

import os
from collections.abc import Sequence

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
        f"there. Unset both (this backend needs neither), or enable only backends "
        f"that perform runtime NAS mounts (ASTRABOX_SANDBOX_BACKEND, "
        f"ASTRABOX_SANDBOX_BACKENDS)."
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
        f"enable only backends that can pause (ASTRABOX_SANDBOX_BACKEND, "
        f"ASTRABOX_SANDBOX_BACKENDS)."
    )


def _assert_git_https_token_is_honored(*backend_names: str) -> None:
    """Refuse a Git HTTPS token that no clone reads or that names no host.

    The token is added only when an SSH repository is cloned over HTTPS on a
    backend that cannot reach Git over SSH, and only to a clone of
    ``ASTRABOX_GIT_HTTPS_TOKEN_HOST``. When none of ``backend_names`` (the
    enabled backends) clones that way, nothing reads it. Without a host, the
    host an Agent's author names would decide where the deployment's token goes.
    Both are refused rather than kept.
    """
    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.seams.sandbox import sandbox_for_name

    settings = load_astrabox_settings()
    name = str(settings.git_https_token_secret_name or "").strip()
    host = str(settings.git_https_token_host or "").strip()
    if not name and not host:
        return
    if not name or not host:
        raise BootstrapConfigError(
            "ASTRABOX_GIT_HTTPS_TOKEN_SECRET_NAME and ASTRABOX_GIT_HTTPS_TOKEN_HOST "
            "are set together: the token authenticates to one Git host, and a "
            "clone of any other host must not receive it. Set both, or neither."
        )
    providers = [sandbox_for_name(backend_name) for backend_name in backend_names]
    if any(provider.requires_https_git for provider in providers):
        return
    names = ", ".join(repr(provider.name) for provider in providers)
    raise BootstrapConfigError(
        f"ASTRABOX_GIT_HTTPS_TOKEN_SECRET_NAME is set, but the enabled sandbox "
        f"backends ({names}) clone SSH repositories over SSH, so no clone uses the "
        f"token. Unset it. A private HTTPS Skill or Plugin repository "
        f"authenticates through an http_basic Vault credential assigned to the "
        f"Agent."
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


def _assert_title_model_credential_target() -> None:
    """Refuse a title base URL that could only receive the gateway credential.

    Title and process-summary requests without their own key present the
    server's model gateway credential, which may go only to the gateway
    itself. Refusing here names both settings at startup instead of failing
    every conversation's title.
    """

    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.core.service.orchestrator.session_title_service import (
        SessionTitleGenerationError,
        validate_title_model_settings,
    )

    try:
        validate_title_model_settings(load_astrabox_settings())
    except SessionTitleGenerationError as exc:
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


def _assert_extension_provider_is_registered() -> None:
    """Refuse to boot on an extension provider name nothing registered.

    The setting is otherwise first read when someone opens an Agent's catalog,
    where a misspelt ``none`` reads as a broken console rather than as the
    configuration mistake it is.
    """
    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.seams.extensions import (
        EXTENSION_PROVIDER_NONE,
        registered_extension_provider_names,
        selected_extension_provider,
    )

    setting = load_astrabox_settings().extension_provider
    try:
        selected_extension_provider(setting)
    except RuntimeError as exc:
        raise BootstrapConfigError(
            f"ASTRABOX_EXTENSION_PROVIDER={setting!r}: {exc}. Name an installed "
            f"provider ({registered_extension_provider_names()}), or "
            f"{EXTENSION_PROVIDER_NONE!r} to run without an extension catalog."
        ) from exc


def _assert_secret_store_is_configured() -> None:
    """Resolve and validate the selected credential store during startup."""
    from astrabox.seams.secrets import SECRET_STORE_ENV, secret_store_for_name

    try:
        secret_store_for_name(None).validate_configuration()
    except RuntimeError as exc:
        raise BootstrapConfigError(f"{SECRET_STORE_ENV}: {exc}") from exc


def _assert_channel_account_resolver_is_configured() -> None:
    """Resolve and validate the selected channel account resolver during startup.

    It is first consulted when a binding that runs as linked accounts drives
    its first message; a misspelled name would otherwise refuse every such
    message long after the deployment looked healthy.
    """
    from astrabox.seams.channel_accounts import (
        CHANNEL_ACCOUNT_RESOLVER_ENV,
        channel_account_resolver,
    )

    try:
        channel_account_resolver().validate_configuration()
    except RuntimeError as exc:
        raise BootstrapConfigError(f"{CHANNEL_ACCOUNT_RESOLVER_ENV}: {exc}") from exc


def _resolve_enabled_sandbox_backends(override: Sequence[str] | None) -> list[str]:
    """The deployment's enabled sandbox backends, refused when they cannot work.

    ``override`` is the embedder's explicit list; ``None`` reads
    ``ASTRABOX_SANDBOX_BACKENDS``. An empty list enables only the default
    backend. Every name must be installed, may appear once, and the list must
    contain the default: an Environment that names no backend resolves to the
    default, and a default this deployment does not enable would refuse every
    such Environment's first Session.
    """
    from astrabox.config.settings import get_settings
    from astrabox.seams.sandbox import default_sandbox_backend, installed_sandbox_names

    installed = installed_sandbox_names()
    default = default_sandbox_backend()
    if not default:
        raise BootstrapConfigError(
            "ASTRABOX_SANDBOX_BACKEND: no default sandbox backend is selected and "
            f"more than one is installed (installed: {installed}); name the default"
        )
    if default not in installed:
        raise BootstrapConfigError(
            f"ASTRABOX_SANDBOX_BACKEND={default!r} is not an installed sandbox "
            f"backend (installed: {installed})"
        )
    names = (
        [str(name or "").strip().lower() for name in override]
        if override is not None
        else get_settings().enabled_sandbox_backends
    )
    if not names:
        return [default]
    if any(not name for name in names):
        raise BootstrapConfigError("ASTRABOX_SANDBOX_BACKENDS contains an empty name")
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise BootstrapConfigError(
            f"ASTRABOX_SANDBOX_BACKENDS names {repeated} more than once"
        )
    missing = [name for name in names if name not in installed]
    if missing:
        raise BootstrapConfigError(
            f"ASTRABOX_SANDBOX_BACKENDS enables {missing}, which no installed "
            f"provider registers (installed: {installed})"
        )
    if default not in names:
        raise BootstrapConfigError(
            f"ASTRABOX_SANDBOX_BACKENDS={','.join(names)} does not enable the "
            f"default backend ASTRABOX_SANDBOX_BACKEND={default!r}; an Environment "
            "that names no backend would be refused. Add it, or choose a default "
            "from the list."
        )
    return names


def _validate_workspace_routes(enabled_backends: Sequence[str]) -> None:
    """A configured workspace volume needs a route on every enabled backend.

    Every persistent create goes through its backend's route, so an enabled
    backend without one would fail at its first conversation instead of here.
    Each distinct route then checks what it needs at startup.
    """
    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.seams.sandbox import sandbox_for_name

    if not str(load_astrabox_settings().sandbox_workspace_volume or "").strip():
        return
    validated: list[object] = []
    for name in enabled_backends:
        provider = sandbox_for_name(name)
        route = provider.workspace_route()
        if route is None:
            raise RuntimeError(
                f"ASTRABOX_SANDBOX_WORKSPACE_VOLUME is set, but sandbox backend "
                f"{provider.name!r} has no workspace route to mount it; unset the "
                "volume or enable only backends that provide one"
            )
        if all(route is not seen for seen in validated):
            route.validate_configuration()
            validated.append(route)


def _settings_field_by_variable() -> dict[str, str]:
    """``AstraBoxRuntimeSettings`` field names keyed by their environment variable."""
    from astrabox.common.utils.settings import AstraBoxRuntimeSettings

    fields: dict[str, str] = {}
    for field_name, field in AstraBoxRuntimeSettings.model_fields.items():
        for choice in getattr(field.validation_alias, "choices", ()):
            if isinstance(choice, str):
                fields[choice] = field_name
    return fields


def _warn_about_settings_of_disabled_backends(enabled_backends: Sequence[str]) -> None:
    """Name each set variable that only an installed but disabled backend reads.

    Such a setting does nothing in this deployment. Startup still proceeds: a
    shared environment template may carry it for deployments that enable that
    backend.
    """
    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.seams.sandbox import installed_sandbox_names, sandbox_if_registered

    fields = _settings_field_by_variable()
    configured = load_astrabox_settings().model_fields_set
    for name in installed_sandbox_names():
        if name in enabled_backends:
            continue
        provider = sandbox_if_registered(name)
        for variable in getattr(provider, "owned_settings", ()):
            field_name = fields.get(variable)
            if field_name is None:
                raise BootstrapConfigError(
                    f"sandbox provider {name!r} declares {variable!r} as one of its "
                    "settings, but AstraBox reads no setting by that name"
                )
            if field_name in configured:
                logger.warning(
                    "%s is set, but only the %r sandbox backend reads it and this "
                    "deployment does not enable that backend (enabled: %s); it has "
                    "no effect",
                    variable,
                    name,
                    list(enabled_backends),
                )


def _note_backends_without_inventory_listing(enabled_backends: Sequence[str]) -> None:
    """Say which enabled backends the background inventory sweeps cannot read.

    The sweeps that reclaim ownerless boxes and retire boxes with stale network
    wiring list a backend's sandboxes. A backend that cannot list them leaves
    an abandoned box to its own expiry.
    """
    from astrabox.seams.sandbox import sandbox_for_name

    for name in enabled_backends:
        if not bool(getattr(sandbox_for_name(name), "supports_inventory_listing", False)):
            logger.warning(
                "sandbox backend %r cannot list its sandboxes: a box it creates "
                "that no AstraBox record names is reclaimed only when the backend "
                "expires it",
                name,
            )


def _select_scheduler_provider() -> None:
    """Select ``ASTRABOX_SCHEDULER_PROVIDER`` and check it against the repository.

    A provider that cannot run with the platform database refuses the start
    here, instead of failing the first schedule a user creates.
    """
    from astrabox.config.settings import get_settings
    from astrabox.seams.scheduler import (
        SchedulerConfigurationError,
        configured_scheduler,
        set_configured_scheduler,
    )

    try:
        set_configured_scheduler(get_settings().scheduler_provider)
    except RuntimeError as exc:
        raise BootstrapConfigError(f"ASTRABOX_SCHEDULER_PROVIDER: {exc}") from exc
    scheduler = configured_scheduler()
    if scheduler is None:
        return
    try:
        database_url = get_settings().resolved_db_url
    except ValueError as exc:
        raise BootstrapConfigError(str(exc)) from exc
    try:
        scheduler.configure(database_url=database_url)
    except SchedulerConfigurationError as exc:
        raise BootstrapConfigError(
            f"ASTRABOX_SCHEDULER_PROVIDER={scheduler.name} cannot run with this "
            f"database: {exc}. Select another scheduler provider, or set "
            "ASTRABOX_SCHEDULER_PROVIDER=none to disable scheduled Deployments."
        ) from exc


def bootstrap(
    *,
    sandbox_backend: str | None = None,
    sandbox_backends: Sequence[str] | None = None,
) -> None:
    """Compose the provider registries (idempotent; see module docstring).

    ``sandbox_backend`` overrides the settings-resolved default backend name;
    ``None`` publishes ``get_settings().sandbox_backend`` exactly like the app
    lifespan does. ``sandbox_backends`` overrides the enabled backend list
    (``ASTRABOX_SANDBOX_BACKENDS``); ``None`` reads the setting.
    """
    global _bootstrapped
    from astrabox.config.settings import get_settings
    from astrabox.providers import (
        load_entry_point_providers,
        register_builtin_providers,
    )
    from astrabox.persistence.installation import load_installation_id
    from astrabox.seams.sandbox import (
        set_default_sandbox_backend,
        set_enabled_sandbox_backends,
        set_sandbox_installation_loader,
    )
    from astrabox.seams.storage import set_configured_storage_provider

    register_builtin_providers()
    load_entry_point_providers()
    effective_backend = (
        sandbox_backend if sandbox_backend is not None else get_settings().sandbox_backend
    )
    set_default_sandbox_backend(effective_backend)
    enabled_backends = _resolve_enabled_sandbox_backends(sandbox_backends)
    set_enabled_sandbox_backends(enabled_backends)
    # The installation id lives in the database, so it is read on first use,
    # not here: composing the registries must not need a reachable database.
    set_sandbox_installation_loader(load_installation_id)
    # Not derived from the sandbox backend: where a workspace lives is a
    # durability decision, not a runtime one, which is why storage is keyed by
    # its own name.
    set_configured_storage_provider(get_settings().storage_provider)
    _assert_secret_store_is_configured()
    _assert_channel_account_resolver_is_configured()
    _assert_storage_provider_is_registered()
    _assert_extension_provider_is_registered()
    _select_scheduler_provider()
    try:
        _validate_workspace_routes(enabled_backends)
    except (RuntimeError, ValueError) as exc:
        raise BootstrapConfigError(f"workspace routing: {exc}") from exc
    for backend_name in enabled_backends:
        _assert_nas_knobs_are_honored(backend_name)
        _assert_idle_action_is_honored(backend_name)
    _assert_git_https_token_is_honored(*enabled_backends)
    _assert_model_gateway_https_requirement()
    _assert_title_model_credential_target()
    _warn_about_settings_of_disabled_backends(enabled_backends)
    _note_backends_without_inventory_listing(enabled_backends)
    if not _bootstrapped:
        logger.info("astrabox provider composition bootstrapped")
    _bootstrapped = True
