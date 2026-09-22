"""Provider registry — entry-point loading, fail-loud.

A "provider" is a concrete implementation of one of the ``Protocol`` seams declared
in :mod:`astrabox.seams`. Providers register themselves under PEP 621 entry-point
groups (declared in ``pyproject.toml``); the host resolves a provider *by name* and
**never falls back silently** — an empty / unknown / unregistered name is a hard
error that surfaces the available names.

The built-in provider is ``open_sandbox`` (sandbox lifecycle over the OpenSandbox
HTTP API). Additional adapters can ship as a *separate distribution* that registers
at the **same** entry-point groups without touching this package.

Two surfaces live here:

* :func:`get_sandbox_provider` — load + instantiate the sandbox provider selected
  by name (or the single registered one), raising loud on miss.
* :func:`register_builtin_providers` — eagerly import the built-in
  ``open_sandbox`` seam modules for their ``register_*`` side effects
  (consumer-driven registration). Called once at app bootstrap.

The sandbox-backend default is ``"open_sandbox"``, selected explicitly at
the call sites (never by silent default here).
"""

from __future__ import annotations

from importlib.metadata import EntryPoint, entry_points
from typing import Any

# Entry-point groups — must match the ``[project.entry-points."..."]`` tables in
# pyproject.toml. One group per Protocol seam.
SANDBOX_GROUP = "astrabox.providers.sandbox"
STORAGE_GROUP = "astrabox.providers.storage"
ENGINE_GROUP = "astrabox.providers.engine"
MODEL_GROUP = "astrabox.providers.model"
SECRETS_GROUP = "astrabox.providers.secrets"
CHANNEL_GROUP = "astrabox.providers.channel"
ADMISSION_GROUP = "astrabox.providers.admission"
EXTENSIONS_GROUP = "astrabox.providers.extensions"

# Groups whose members are eagerly loaded at bootstrap so their register_* side
# effects (or class registrations) run without the host importing the plugin
# module by name. The repository group is deliberately absent: repository
# backends are lazily selected by name (astrabox/repositories) and must not be
# instantiated eagerly.
_EAGER_GROUPS = (
    SANDBOX_GROUP,
    STORAGE_GROUP,
    ENGINE_GROUP,
    MODEL_GROUP,
    SECRETS_GROUP,
    CHANNEL_GROUP,
    ADMISSION_GROUP,
    EXTENSIONS_GROUP,
)

# The single built-in provider name (every seam registers under it).
OPEN_SANDBOX = "open_sandbox"


def _select_entry_points(group: str) -> dict[str, EntryPoint]:
    """Return ``{name: entry_point}`` for one entry-point ``group``.

    Uses the modern ``entry_points(group=...)`` selection API (Python 3.10+;
    ``importlib.metadata``). Duplicate names within a group are a packaging error
    and fail loud rather than letting one silently shadow another.
    """
    selected: dict[str, EntryPoint] = {}
    for ep in entry_points(group=group):
        if ep.name in selected:
            raise RuntimeError(
                f"duplicate provider entry-point name={ep.name!r} in group={group!r} "
                f"(already from {selected[ep.name].value!r}, now {ep.value!r}); "
                f"resolve the packaging conflict — no silent shadowing"
            )
        selected[ep.name] = ep
    return selected


def _check_seams_api_version(target: Any, *, group: str, name: str) -> Any:
    """Reject a provider built against an incompatible seams contract, at load time.

    A provider distribution MAY pin the contract it was built against by setting
    ``seams_api_version = <int>`` on its provider class. When set and different
    from the running :data:`astrabox.seams.SEAMS_API_VERSION`, registration fails
    HERE with a clear message naming the mismatch, catching a renamed Protocol
    method before it can surface as a mid-turn ``AttributeError``. Absent
    attribute → no check (pinning is opt-in).
    """
    declared = getattr(target, "seams_api_version", None)
    if declared is None:
        return target
    from astrabox.seams import SEAMS_API_VERSION

    if int(declared) != int(SEAMS_API_VERSION):
        raise RuntimeError(
            f"provider {name!r} (group={group!r}) was built against seams API "
            f"version {declared}, but this AstraBox provides {SEAMS_API_VERSION}; "
            "upgrade the plugin distribution (or AstraBox) so the versions match"
        )
    return target


def load_provider(group: str, name: str | None = None) -> Any:
    """Load + instantiate one provider from an entry-point ``group``, fail-loud.

    * ``name`` given → that exact provider, or ``RuntimeError`` listing the
      registered names if it is not present (no fallback to "the first one").
    * ``name`` omitted/empty and exactly one provider registered → that one.
    * ``name`` omitted and zero or many registered → ``RuntimeError`` (ambiguous /
      none; the caller must select explicitly).

    The loaded object is the entry-point target; for the seam classes that target
    is the adapter class (the caller instantiates it — see
    :func:`get_sandbox_provider`). A target that pins ``seams_api_version`` is
    version-checked against :data:`astrabox.seams.SEAMS_API_VERSION` (fail-loud
    on mismatch).
    """
    available = _select_entry_points(group)
    if not available:
        raise RuntimeError(
            f"no providers registered for entry-point group={group!r}; "
            f"install a provider distribution (AstraBox ships {OPEN_SANDBOX!r}) "
            f"or call register_builtin_providers() before resolving"
        )

    wanted = str(name or "").strip().lower()
    if not wanted:
        if len(available) != 1:
            raise RuntimeError(
                f"provider name required for group={group!r}: "
                f"{len(available)} registered ({sorted(available)}); "
                f"select one explicitly — there is no default"
            )
        ((only_name, only_ep),) = available.items()
        return _check_seams_api_version(only_ep.load(), group=group, name=only_name)

    ep = available.get(wanted)
    if ep is None:
        raise RuntimeError(
            f"no provider named {wanted!r} in group={group!r} "
            f"(registered: {sorted(available)})"
        )
    return _check_seams_api_version(ep.load(), group=group, name=wanted)


def get_sandbox_provider(name: str | None = None) -> Any:
    """Factory for the sandbox provider (the task-named entry point).

    Resolves the ``astrabox.providers.sandbox`` entry-point group, loads the
    selected target (a :class:`SandboxProvider` subclass), and returns a
    **constructed instance**. Raises loud when nothing is registered or the name is
    unknown — there is no silent fallback to a default backend.

    With no ``name`` and the default install (only ``open_sandbox`` registered)
    this returns an ``OpenSandboxSandboxProvider()``.
    """
    target = load_provider(SANDBOX_GROUP, name)
    # Seam entry-points point at the adapter *class*; instantiate it.
    return target() if isinstance(target, type) else target


def register_builtin_providers() -> None:
    """Import the built-in seam modules for their registration side effects.

    Each built-in seam module, on import, calls the matching ``register_*``:
    the ``open_sandbox`` sandbox provider and the ``local`` storage provider ->
    ``register_sandbox`` / ``register_storage`` (:mod:`astrabox.seams`), the model + secret-store
    modules register their providers, and the two built-in engine adapters
    (``claude_code`` / ``assistant``) call ``register_engine_adapter``
    (:mod:`astrabox.core.service.orchestrator.engine.registry`). Together these
    populate the in-process registries that ``runtime_manager`` and the turn
    pipeline read. The sandbox provider also owns the in-box transport
    (``build_dataplane``), so no separate registration exists for that. This
    performs all consumer-driven registration in a single call.

    Engines are included here as the in-tree fallback that mirrors every other
    seam: this call guarantees the built-ins register in-proc (source checkouts
    / unit tests / any path importing ``runtime_manager`` without running app
    bootstrap), while :func:`load_entry_point_providers` is the plugin-discovery
    path that also picks these up by their entry-points. The engine
    entry-points target these SAME core modules, so both paths resolve to one
    class and registration stays idempotent.

    Importing is idempotent (Python module cache) and the registries themselves
    are append/overwrite-by-name, so calling this more than once is safe.
    """
    # Local imports: importing triggers each module's register_* side effect.
    # Each module populates the matching astrabox.seams registry.
    from astrabox.providers.open_sandbox import sandbox as _open_sandbox_sandbox  # noqa: F401
    from astrabox.providers.storage import (  # noqa: F401
        aws_efs as _aws_efs_storage,
        mounted_volume as _mounted_volume_storage,
    )
    from astrabox.providers import model as _model  # noqa: F401
    from astrabox.providers import secret_store as _secret_store  # noqa: F401
    from astrabox.providers import secret_store_aws_kms as _aws_kms_secret_store  # noqa: F401
    from astrabox.providers import channel_generic as _channel_generic  # noqa: F401
    from astrabox.providers import channel_satori as _channel_gateway  # noqa: F401
    from astrabox.providers import builtin_extensions as _builtin_extensions  # noqa: F401
    from astrabox.providers import litellm_extensions as _extensions  # noqa: F401

    # Engines: importing each adapter module runs its bottom-line
    # ``register_engine_adapter(...)``. This is the in-tree fallback so the
    # built-in engines register in-proc without app bootstrap; the
    # ``astrabox.providers.engine`` entry-points point at these SAME modules, so
    # the entry-point load and this import resolve to one class (idempotent).
    from astrabox.core.service.orchestrator.engine import (  # noqa: F401
        claude_code as _claude_engine,
        codex as _codex_engine,
        deepseek_harness as _deepseek_harness_engine,
        hermes as _hermes_engine,
        pi as _pi_engine,
    )

    # ``sandbox_for_name`` has no "sole registered provider" fallback: an empty
    # backend name resolves ONLY through the published process default, however
    # many backends are registered. So publish the deployment-configured default
    # whenever none is configured yet — the SAME settings source bootstrap
    # publishes explicitly after this call and takes precedence; an
    # already-configured default is never overridden here. Without
    # this, any process that imports the runtime without running bootstrap loses
    # empty-name resolution entirely.
    from astrabox.config.settings import get_settings
    from astrabox.seams.sandbox import (
        default_sandbox_backend,
        set_default_sandbox_backend,
    )

    if not default_sandbox_backend():
        set_default_sandbox_backend(get_settings().sandbox_backend)


def load_entry_point_providers() -> None:
    """Eagerly load every provider advertised at the eager entry-point groups.

    This is what makes the PEP 621 plugin story REAL for sandbox / storage /
    engine / model providers: a third-party distribution that declares an entry
    point in one of those groups is loaded here at bootstrap — its import runs
    its ``register_*`` side effect, and a target that is a class with a ``name``
    not yet registered is instantiated and registered explicitly. Without this
    call only in-tree imports would populate the registries, and an installed
    plugin would silently never activate.

    Fail-loud: a broken entry point raises with the group + name in the message
    rather than being skipped.
    """
    from astrabox.seams.channel import channel_if_registered, register_channel
    from astrabox.seams.extensions import (
        register_extension_provider,
        registered_extension_provider_names,
    )
    from astrabox.seams.model import (
        register_model_endpoint,
        registered_model_endpoint_names,
    )
    from astrabox.seams.sandbox import register_sandbox, sandbox_if_registered
    from astrabox.seams.storage import _PROVIDERS as _STORAGE_PROVIDERS  # registry view
    from astrabox.seams.storage import register_storage

    for group in _EAGER_GROUPS:
        for ep_name, ep in _select_entry_points(group).items():
            try:
                target = ep.load()
            except Exception as exc:
                raise RuntimeError(
                    f"failed to load provider entry point group={group!r} "
                    f"name={ep_name!r} target={ep.value!r}: {exc}"
                ) from exc
            _check_seams_api_version(target, group=group, name=ep_name)
            # Importing usually registered it already (register_* side effect).
            # A class target not yet registered is instantiated + registered here.
            if not isinstance(target, type):
                continue
            if group == ENGINE_GROUP:
                from astrabox.core.service.orchestrator.engine.base import EngineAdapter
                from astrabox.core.service.orchestrator.engine.registry import (
                    register_engine_adapter,
                )

                if not issubclass(target, EngineAdapter):
                    raise RuntimeError(
                        f"engine entry point name={ep_name!r} must target an "
                        "EngineAdapter class"
                    )
                adapter = target()
                adapter_kind = str(adapter.engine_kind or "").strip()
                if adapter_kind != ep_name:
                    raise RuntimeError(
                        f"engine entry point name={ep_name!r} does not match "
                        f"adapter.engine_kind={adapter_kind!r}"
                    )
                register_engine_adapter(adapter_kind, adapter)
                continue
            provider_name = str(getattr(target, "name", "") or "").strip().lower()
            if not provider_name:
                continue
            if group == SANDBOX_GROUP and sandbox_if_registered(provider_name) is None:
                register_sandbox(target())
            elif group == STORAGE_GROUP and provider_name not in _STORAGE_PROVIDERS:
                register_storage(provider_name, target())
            elif group == CHANNEL_GROUP and channel_if_registered(provider_name) is None:
                # Same contract as the sandbox/storage groups: a class target
                # is instantiated + registered here (register_channel fails
                # loud on a partial capability shape).
                register_channel(target())
            elif group == MODEL_GROUP and (
                provider_name not in registered_model_endpoint_names()
            ):
                register_model_endpoint(target())
            elif group == EXTENSIONS_GROUP and (
                provider_name not in registered_extension_provider_names()
            ):
                register_extension_provider(target())


__all__ = [
    "SANDBOX_GROUP",
    "STORAGE_GROUP",
    "ENGINE_GROUP",
    "MODEL_GROUP",
    "SECRETS_GROUP",
    "EXTENSIONS_GROUP",
    "OPEN_SANDBOX",
    "load_provider",
    "get_sandbox_provider",
    "register_builtin_providers",
    "load_entry_point_providers",
]
