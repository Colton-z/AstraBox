"""What an engine lets an Agent or Assistant add to it.

Each engine has its own extension surface, and AstraBox configures that surface
as the engine defines it (``docs/maintainers/engine-extension-contract.md``).
An adapter declares here what it accepts; the platform stores, admits, fetches
and installs from the declaration and names no engine.

Only ``SKILL.md`` Skills and MCP servers are accepted by every bundled engine,
so they are the portable part of a configuration. Everything else is an
engine-native package kind or a native configuration document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Where the engine discovers a kind of item, as a template over the runtime it
#: runs in: ``{home}`` is the runtime's home and ``{config}`` its configuration
#: directory. Both are private to the runtime; nothing is placed in the
#: workspace.
_TARGET_VARIABLES = frozenset({"home", "config"})
_TARGET_VARIABLE = re.compile(r"\{([a-z_]+)\}")

#: The cross-vendor user Skills root (agentskills.io's `~/.agents/skills`),
#: which Codex, Pi and DeepSeek Harness all scan.
AGENTS_HOME_SKILLS_TARGET = "{home}/.agents/skills"

#: When a change to an installed item reaches the engine.
TAKES_EFFECT = frozenset({"next_conversation", "restart", "live"})

#: How a package's files are obtained.
PACKAGE_SOURCES = frozenset({"git", "npm"})

#: Transports an author MCP server definition can name.
MCP_TRANSPORTS = frozenset({"stdio", "http", "sse"})

_KIND_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class PackageKind:
    """One kind of engine-native package, such as a plugin format."""

    #: Stable name stored with each package, unique within the engine.
    name: str
    #: Console label, in the engine's own terms.
    label: str
    #: Where a package of this kind is placed (see ``_TARGET_VARIABLES``).
    target: str
    #: Where its files may come from.
    sources: frozenset[str] = frozenset({"git"})
    #: When a change to it reaches the engine.
    takes_effect: str = "next_conversation"
    #: One sentence naming the vendor format, for the console.
    help: str = ""
    #: True when the engine installs packages of this kind itself (the
    #: adapter's :meth:`~EngineAdapter.conversation_install_script`, or Hermes'
    #: own profile setup, which places its plugins from the Agent cache);
    #: otherwise the conversation bootstrap links each cached checkout under
    #: ``target``.
    installed_by_engine: bool = False
    #: Hosts the engine's installer reaches placing a package of this kind,
    #: beyond the package's own source: Hermes resolves a plugin's declared
    #: Python dependencies from PyPI. They are the author's network hosts,
    #: admitted within the same boundary as the package's Git origin.
    installer_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngineExtensions:
    """An adapter's extension surface."""

    #: Transports accepted for author MCP servers. Empty refuses author
    #: servers; platform MCP servers (artifacts, connections, connectors) are
    #: not author configuration and reach the engine on their own path.
    mcp_transports: frozenset[str] = frozenset()
    #: Where the engine discovers ``SKILL.md`` Skills. ``None`` refuses Skills.
    skills_target: str | None = None
    #: Engine-native package kinds.
    packages: tuple[PackageKind, ...] = ()
    #: Native configuration documents, by name. Each is an object block with
    #: its label, help and the keys the platform reserves in it; the adapter
    #: applies it at the vendor target its help names.
    native_config: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    #: Whether the engine sends telemetry to a RuntimeProfile's tracing endpoint.
    tracing: bool = False

    def package_kind(self, name: str) -> PackageKind | None:
        return next((kind for kind in self.packages if kind.name == name), None)


def _validate_target(label: str, template: str) -> None:
    if not isinstance(template, str) or not template.strip():
        raise ValueError(f"{label} must be a non-empty path template")
    unknown = sorted(set(_TARGET_VARIABLE.findall(template)) - _TARGET_VARIABLES)
    if unknown:
        raise ValueError(f"{label} names unknown variables {unknown}")
    if not (template.startswith("/") or template.startswith(("{home}", "{config}"))):
        raise ValueError(f"{label} must be absolute or start at {{home}} or {{config}}")
    if "/../" in f"{template}/" or template.endswith("/.."):
        raise ValueError(f"{label} must not climb out of its root")


def render_extension_target(template: str, *, home_dir: str, config_dir: str | None) -> str:
    """A declared target as an absolute path in one runtime."""

    values = {"home": str(home_dir or "").rstrip("/"), "config": str(config_dir or "").rstrip("/")}
    for name in _TARGET_VARIABLE.findall(template):
        if not values.get(name):
            raise ValueError(f"target {template!r} needs the runtime's {name} directory, which it has none of")
    return _TARGET_VARIABLE.sub(lambda match: values[match.group(1)], template)


def validate_engine_extensions(engine_kind: str, extensions: EngineExtensions) -> None:
    """Refuse a malformed declaration when the adapter registers."""

    label = f"engine_kind={engine_kind!r} extensions"
    if not isinstance(extensions, EngineExtensions):
        raise TypeError(f"{label} must be EngineExtensions")
    transports = extensions.mcp_transports
    if not isinstance(transports, frozenset) or not transports <= MCP_TRANSPORTS:
        raise ValueError(f"{label}.mcp_transports must be a subset of {sorted(MCP_TRANSPORTS)}")
    if extensions.skills_target is not None:
        _validate_target(f"{label}.skills_target", extensions.skills_target)
    if not isinstance(extensions.packages, tuple):
        raise TypeError(f"{label}.packages must be a tuple")
    names: set[str] = set()
    for kind in extensions.packages:
        kind_label = f"{label}.packages[{getattr(kind, 'name', '?')!r}]"
        if not isinstance(kind, PackageKind):
            raise TypeError(f"{kind_label} must be PackageKind")
        if _KIND_NAME.fullmatch(str(kind.name or "")) is None:
            raise ValueError(f"{kind_label} name must be lower-case words joined by hyphens")
        if kind.name in names:
            raise ValueError(f"{kind_label} is declared twice")
        names.add(kind.name)
        if not str(kind.label or "").strip():
            raise ValueError(f"{kind_label} needs a label")
        _validate_target(f"{kind_label}.target", kind.target)
        if not isinstance(kind.sources, frozenset) or not kind.sources or not kind.sources <= PACKAGE_SOURCES:
            raise ValueError(f"{kind_label}.sources must be a non-empty subset of {sorted(PACKAGE_SOURCES)}")
        if kind.takes_effect not in TAKES_EFFECT:
            raise ValueError(f"{kind_label}.takes_effect must be one of {sorted(TAKES_EFFECT)}")
        if not isinstance(kind.installer_hosts, tuple) or not all(
            isinstance(host, str) and host and host == host.strip().lower() for host in kind.installer_hosts
        ):
            raise ValueError(f"{kind_label}.installer_hosts must be a tuple of lower-case host names")
    if not isinstance(extensions.tracing, bool):
        raise TypeError(f"{label}.tracing must be a boolean")


def accepted_configuration_inputs(extensions: EngineExtensions) -> frozenset[str]:
    """The Agent and RuntimeProfile fields this surface gives a consumer."""

    accepted: set[str] = set()
    if extensions.mcp_transports:
        accepted.add("mcp_servers")
    if extensions.skills_target is not None:
        accepted.add("skills")
    if extensions.packages:
        accepted.add("packages")
    if extensions.tracing:
        accepted.add("tracing")
    return frozenset(accepted)


def extensions_projection(extensions: EngineExtensions) -> dict[str, Any]:
    """The surface as the console reads it: what to offer, in vendor terms."""

    return {
        "mcp_transports": sorted(extensions.mcp_transports),
        "skills": extensions.skills_target is not None,
        "packages": [
            {"name": kind.name, "label": kind.label, "help": kind.help,
             "sources": sorted(kind.sources), "takes_effect": kind.takes_effect}
            for kind in extensions.packages
        ],
        "tracing": extensions.tracing,
    }
