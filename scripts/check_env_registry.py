#!/usr/bin/env python3
"""Every environment variable AstraBox reads has a registry row, and every row a read.

``astrabox/config/env_registry.py`` is the census of the variables an operator
can set; ``docs/configuration.md`` is generated from it. A read with no row is a
setting nobody can find, and a row with no read is a knob that reaches no code —
the "no inert knobs" rule of this repository. Both directions are checked,
because one alone lets the registry and the code drift apart from the other end:

* **Completeness.** An AST scan of every ``.py`` file under ``astrabox/`` and
  ``scripts/runtime/`` finds each ``os.getenv`` / ``os.environ[...]`` /
  ``os.environ.get`` with a literal name, a name a module-level constant or a
  ``for`` loop over a literal tuple resolves, and the first argument of the
  wrappers in ``_WRAPPER_CALLS``. The ``AstraBoxSettings`` and
  ``AstraBoxRuntimeSettings`` fields are read by pydantic-settings, which no
  call site shows, so they are resolved from the live classes. Every name found
  must have a row.
* **No stale rows.** Every row must match a name the scan finds, a name an
  embedded sandbox script reads, a name the bundled LiteLLM configuration reads,
  or one of ``_EXTERNAL_RUNTIME_ENV_READS``.
* **No new dynamic reads.** A read whose name the scan cannot resolve is refused
  unless it is listed in ``_ALLOWED_UNRESOLVED`` with a pattern row in the
  registry; a listed site that has gone is reported too.
* **Embedded scripts.** A name read inside a script shipped into the sandbox as
  a string (a bash heredoc, a Python shim) is invisible to the AST scan, so the
  scripts in ``_embedded_scripts`` are searched for literal names.
* **What Compose forwards.** Compose reads ``containers/.env`` only to
  interpolate its files: a variable the ``server`` service does not name never
  reaches the server. Every variable a server Compose file forwards must have a
  row, and the selections in ``_COMPOSE_FORWARDS`` must be forwarded with the
  setting's own default, so an unset value selects what a server started
  without it selects.
* **Typed settings are read.** Every ``AstraBoxRuntimeSettings`` field must be
  read somewhere outside its declaration module.
* **Literal copies agree.** ``astrabox.deploy.onebox`` keeps literal copies of
  provider constants so this scan can resolve its reads; each copy must equal
  the constant it mirrors.
* **The generated reference is fresh.** ``docs/configuration.md`` must be what
  ``scripts/gen_config_docs.py`` renders from the current registry.

Run it directly to see every finding:

    .venv/bin/python scripts/check_env_registry.py
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "astrabox"
#: Python programs the sandbox images install and run. They read their
#: configuration from the environment the platform writes, so they are read
#: sites exactly like the package itself, only not imported by it.
IMAGE_PROGRAMS_ROOT = REPO_ROOT / "scripts" / "runtime"

sys.path.insert(0, str(REPO_ROOT))

from astrabox.config.env_registry import concrete_names  # noqa: E402

#: Wrapper functions that compose an env-var name from a literal argument, so a
#: plain search for ``os.getenv`` never sees their call sites. Keyed by
#: (defining module, function name); the value is the index of the literal-name
#: argument at each call site.
_WRAPPER_CALLS: dict[tuple[str, str], int] = {
    ("astrabox.providers.identity_sso", "_env"): 0,
    ("astrabox.providers.identity_sso", "_strict_flag"): 0,
    ("astrabox.providers.identity_oidc", "_env"): 0,
    ("astrabox.identity.oidc", "_env"): 0,
    ("astrabox.deploy.sandbox_server", "_env"): 0,
    ("astrabox.deploy.sandbox_server", "_flag"): 0,
    ("astrabox.deploy.onebox", "_env"): 0,
    ("astrabox.deploy.onebox", "export_default"): 0,
}

#: The wrappers' own bodies read ``os.getenv(<parameter>)``. That read is the
#: call sites above, not a new finding, so it is skipped.
_WRAPPER_IMPL_FUNCS: dict[str, frozenset[str]] = {
    "astrabox.providers.identity_sso": frozenset({"_env", "_strict_flag"}),
    "astrabox.providers.identity_oidc": frozenset({"_env"}),
    "astrabox.identity.oidc": frozenset({"_env"}),
    "astrabox.deploy.sandbox_server": frozenset({"_env", "_flag"}),
    "astrabox.deploy.onebox": frozenset({"_env", "export_default"}),
}

#: The one genuinely dynamic read: ``astrabox/secrets.py``'s
#: ``SecretProvider.get_secret`` reads ``os.getenv(secret_env_key(name))`` for a
#: name its caller supplies at runtime. The registry documents it as
#: ``env_registry.SECRET_NAME_PATTERN_ROW`` instead of a literal row.
_ALLOWED_UNRESOLVED: frozenset[tuple[str, int]] = frozenset({("astrabox.secrets", 42)})

#: Names read by pinned third-party processes or image-owned clients, where no
#: ``os.environ`` expression of this repository can show the read. Hermes
#: receives its custom provider key in a private profile env file; the bundled
#: gateway's Langfuse export reads the three observability values; the pi image
#: renders the vendor's models.json from its base URL and model, and pi resolves
#: the credential from the environment at request time (models.json carries the
#: variable's NAME, so the value never lands on disk); the image-owned
#: JavaScript clients of both engines read the platform MCP servers.
_EXTERNAL_RUNTIME_ENV_READS: frozenset[str] = frozenset(
    {
        "ASTRABOX_HERMES_MODEL_API_KEY",
        "ASTRABOX_PI_API_KEY",
        "ASTRABOX_PI_BASE_URL",
        "ASTRABOX_PI_MODEL",
        "ASTRABOX_PLATFORM_MCP_SERVERS",
        "LANGFUSE_HOST",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    }
)

#: Server selections Compose must forward with the setting's own default, so an
#: unset or empty value in ``containers/.env`` selects what a server started
#: without it selects.
_COMPOSE_FORWARDS: dict[str, str] = {
    "ASTRABOX_SCHEDULER_PROVIDER": "${ASTRABOX_SCHEDULER_PROVIDER:-dbos}",
    "ASTRABOX_SANDBOX_BACKENDS": "${ASTRABOX_SANDBOX_BACKENDS:-}",
}


# ── the AST scan ─────────────────────────────────────────────────────────────


def _module_dotted(path: Path) -> str:
    """``astrabox/foo/bar.py`` (repository-relative) as ``astrabox.foo.bar``."""
    return ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)


def _literal_str(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _module_literals(tree: ast.Module) -> dict[str, Any]:
    """Module-level ``NAME = <literal>`` assignments, evaluated.

    They resolve a module constant used as an env-read argument, and a ``for``
    loop over a module-level tuple of literal names or pairs.
    """
    out: dict[str, Any] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and value is not None:
            try:
                out[target.id] = ast.literal_eval(value)
            except Exception:
                pass
    return out


@dataclass
class ScanResult:
    #: name -> {(module, line, how), ...}
    sites: dict[str, set[tuple[str, int, str]]] = field(default_factory=dict)
    #: (module, line) of a read whose argument resolves to no literal.
    unresolved: set[tuple[str, int]] = field(default_factory=set)

    @property
    def names(self) -> set[str]:
        return set(self.sites)

    def add(self, name: str, module: str, line: int, how: str) -> None:
        self.sites.setdefault(name, set()).add((module, line, how))


class _ModuleScanner(ast.NodeVisitor):
    """Finds the env-var reads of one module."""

    def __init__(self, module: str, result: ScanResult, literals: dict[str, Any]) -> None:
        self.module = module
        self.result = result
        self.literals = literals
        self._functions: list[str] = []
        self._scopes: list[dict[str, frozenset[str]]] = []

    def _in_wrapper_body(self) -> bool:
        skip = _WRAPPER_IMPL_FUNCS.get(self.module, frozenset())
        return any(name in skip for name in self._functions)

    def _resolve(self, node: ast.Name) -> list[str] | None:
        for scope in reversed(self._scopes):
            if node.id in scope:
                return sorted(scope[node.id])
        value = self.literals.get(node.id)
        return [value] if isinstance(value, str) else None

    def _handle(self, node: ast.AST, arg: ast.expr, how: str) -> None:
        if self._in_wrapper_body():
            return
        line = getattr(node, "lineno", 0)
        literal = _literal_str(arg)
        if literal is not None:
            self.result.add(literal, self.module, line, how)
            return
        if isinstance(arg, ast.Name):
            resolved = self._resolve(arg)
            if resolved is not None:
                for name in resolved:
                    self.result.add(name, self.module, line, f"{how} (resolved)")
                return
        self.result.unresolved.add((self.module, line))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _iterated(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Name):
            return self.literals.get(node.id)
        try:
            return ast.literal_eval(node)
        except Exception:
            return None

    def visit_For(self, node: ast.For) -> None:
        # ``for key in ("A", "B"):`` or ``for a, b in TABLE:`` binds the loop
        # variables to the literals they range over, for the loop body.
        binding: dict[str, frozenset[str]] = {}
        values = self._iterated(node.iter)
        if isinstance(values, (list, tuple)) and values:
            if isinstance(node.target, ast.Name):
                strings = frozenset(v for v in values if isinstance(v, str))
                if strings:
                    binding[node.target.id] = strings
            elif isinstance(node.target, (ast.Tuple, ast.List)):
                for index, element in enumerate(node.target.elts):
                    if not isinstance(element, ast.Name):
                        continue
                    column = frozenset(
                        row[index]
                        for row in values
                        if isinstance(row, (list, tuple))
                        and len(row) > index
                        and isinstance(row[index], str)
                    )
                    if column:
                        binding[element.id] = column
        if binding:
            self._scopes.append(binding)
        self.generic_visit(node)
        if binding:
            self._scopes.pop()

    def visit_Subscript(self, node: ast.Subscript) -> None:
        value = node.value
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "environ"
            and isinstance(value.value, ast.Name)
            and value.value.id == "os"
        ):
            self._handle(node, node.slice, "os.environ[]")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "getenv"
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
            and node.args
        ):
            self._handle(node, node.args[0], "os.getenv")
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "environ"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os"
            and node.args
        ):
            self._handle(node, node.args[0], "os.environ.get")
        elif isinstance(func, ast.Name):
            index = _WRAPPER_CALLS.get((self.module, func.id))
            if index is not None and len(node.args) > index:
                self._handle(node, node.args[index], func.id)
        self.generic_visit(node)


def _add_settings_fields(result: ScanResult) -> None:
    """Names pydantic-settings reads for the two typed settings classes.

    Read off the live classes' own alias and prefix rules rather than a re-parse
    of how ``Field(...)`` is written. A field with only an ``AliasPath`` (a
    YAML-only field) reads no environment variable and contributes nothing.
    """
    from pydantic import AliasChoices

    from astrabox.common.utils.settings import AstraBoxRuntimeSettings
    from astrabox.config.settings import AstraBoxSettings

    for settings_cls in (AstraBoxSettings, AstraBoxRuntimeSettings):
        prefix = str(settings_cls.model_config.get("env_prefix") or "")
        module = settings_cls.__module__
        for field_name, info in settings_cls.model_fields.items():
            alias = info.validation_alias
            if isinstance(alias, AliasChoices):
                names = [choice for choice in alias.choices if isinstance(choice, str)]
            elif isinstance(alias, str):
                names = [alias]
            else:
                names = [(prefix + field_name).upper()]
            for name in names:
                result.add(name, module, 0, f"pydantic-field:{field_name}")


def scan_env_reads() -> ScanResult:
    result = ScanResult()
    paths = [*PACKAGE_ROOT.rglob("*.py"), *IMAGE_PROGRAMS_ROOT.glob("*.py")]
    for path in sorted(paths):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _ModuleScanner(_module_dotted(path), result, _module_literals(tree)).visit(tree)
    _add_settings_fields(result)
    return result


# ── names read where the AST scan cannot look ────────────────────────────────

_ENV_NAME = r"[A-Z][A-Z0-9_]*"
_RE_ENVIRON_GET = re.compile(rf"os\.environ\.get\(\s*[\"']({_ENV_NAME})[\"']")
_RE_ENVIRON_GETITEM = re.compile(rf"os\.environ\[\s*[\"']({_ENV_NAME})[\"']\s*\]")
_RE_ENVIRON_CONTAINS = re.compile(rf"[\"']({_ENV_NAME})[\"']\s+in\s+os\.environ")
# A ``keys = [...]`` / ``keys += [...]`` list of quoted names (a gateway script's
# forwarding allowlist), scoped to the bracketed block so an unrelated quoted
# string elsewhere in the script is never taken for a name.
_RE_KEYS_LIST_BLOCK = re.compile(r"\bkeys\s*\+?=\s*\[(.*?)\]", re.DOTALL)
_RE_QUOTED_NAME = re.compile(rf"[\"']({_ENV_NAME})[\"']")
# bash ``${NAME}`` / ``${NAME:-default}``, limited to the namespaces these
# scripts use so a local shell variable never matches.
_RE_BASH_BRACE = re.compile(
    r"\$\{(ASTRABOX_[A-Z0-9_]+|HERMES_[A-Z0-9_]+|API_SERVER_[A-Z0-9_]+|"
    r"CONV_[A-Z0-9_]+|GIT_HTTPS_TOKEN)(?::[^}]*)?\}"
)
# bash ``$(required NAME)``: the fail-loud accessor takes the name as a bare
# argument, which neither family above sees.
_RE_BASH_REQUIRED = re.compile(
    r"\b(?:required|require_env)\s+"
    r"(ASTRABOX_[A-Z0-9_]+|HERMES_[A-Z0-9_]+|API_SERVER_[A-Z0-9_]+|"
    r"CONV_[A-Z0-9_]+|GIT_HTTPS_TOKEN)\b"
)


def _embedded_scripts() -> dict[str, str]:
    """Scripts shipped into the sandbox as text, which read their own environment."""
    from astrabox.core.service.orchestrator.engine.hermes import _HERMES_PROFILE_SETUP_SCRIPT

    runtime = REPO_ROOT / "astrabox/core/service/orchestrator/runtime"
    hermes = REPO_ROOT / "containers/sandbox-hermes"
    return {
        "hermes._HERMES_PROFILE_SETUP_SCRIPT": _HERMES_PROFILE_SETUP_SCRIPT,
        "runtime/astrabox-assistant-workspace-storage": (
            runtime / "astrabox-assistant-workspace-storage"
        ).read_text(encoding="utf-8"),
        "runtime/provision-conversation": (runtime / "provision-conversation").read_text(
            encoding="utf-8"
        ),
        "runtime/astrabox-transcript-mirror": (runtime / "astrabox-transcript-mirror").read_text(
            encoding="utf-8"
        ),
        "sandbox-hermes/astrabox-hermes-serve": (hermes / "astrabox-hermes-serve").read_text(
            encoding="utf-8"
        ),
        "sandbox-hermes/astrabox-hermes-forward": (hermes / "astrabox-hermes-forward").read_text(
            encoding="utf-8"
        ),
    }


def _script_env_names(script: str) -> set[str]:
    names: set[str] = set()
    for pattern in (
        _RE_ENVIRON_GET,
        _RE_ENVIRON_GETITEM,
        _RE_ENVIRON_CONTAINS,
        _RE_BASH_BRACE,
        _RE_BASH_REQUIRED,
    ):
        names.update(match.group(1) for match in pattern.finditer(script))
    for block in _RE_KEYS_LIST_BLOCK.finditer(script):
        names.update(match.group(1) for match in _RE_QUOTED_NAME.finditer(block.group(1)))
    return names


def _bundled_litellm_reads() -> frozenset[str]:
    config = (REPO_ROOT / "containers/litellm/config.yaml").read_text(encoding="utf-8")
    return frozenset(re.findall(r"\bos\.environ/([A-Z][A-Z0-9_]*)\b", config))


class _ComposeLoader(yaml.SafeLoader):
    """Reads Compose's ``!override`` and ``!reset`` tags as plain values."""


def _construct_override(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor("!override", _construct_override)
_ComposeLoader.add_constructor("!reset", lambda loader, node: None)


def _compose_server_environments() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((REPO_ROOT / "containers").glob("compose*.yaml")):
        compose = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)
        environment = (compose.get("services", {}).get("server", {}) or {}).get("environment")
        if environment:
            out[path.name] = dict(environment)
    return out


def _unread_runtime_settings() -> list[str]:
    """``AstraBoxRuntimeSettings`` fields no module outside the declaration reads."""
    from astrabox.common.utils.settings import AstraBoxRuntimeSettings

    unread = set(AstraBoxRuntimeSettings.model_fields)
    declaration = PACKAGE_ROOT / "common" / "utils" / "settings.py"
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path == declaration:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                unread.discard(node.attr)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                unread.discard(node.args[1].value)
    return sorted(unread)


def _diverged_onebox_copies() -> list[str]:
    """Literal copies in ``onebox`` that differ from the constants they mirror."""
    from astrabox.deploy import onebox
    from astrabox.providers import channel_gateway, model

    pairs = (
        ("LITELLM_BASE_URL_ENV_NAME", model.LITELLM_BASE_URL_ENV),
        ("LITELLM_API_KEY_ENV_NAME", model.LITELLM_API_KEY_ENV),
        ("CHANNEL_GATEWAY_BASE_URL_ENV", channel_gateway.CHANNEL_GATEWAY_BASE_URL_ENV),
        ("CHANNEL_GATEWAY_TOKEN_ENV", channel_gateway.CHANNEL_GATEWAY_TOKEN_ENV),
        ("CHANNEL_GATEWAY_HOST_ENV", channel_gateway.CHANNEL_GATEWAY_HOST_ENV),
        ("CHANNEL_GATEWAY_PORT_ENV", channel_gateway.CHANNEL_GATEWAY_PORT_ENV),
        ("CHANNEL_GATEWAY_MANIFEST_ENV", channel_gateway.CHANNEL_GATEWAY_MANIFEST_ENV),
    )
    return [
        f"onebox.{name} = {getattr(onebox, name)!r}, the provider's is {value!r}"
        for name, value in pairs
        if getattr(onebox, name) != value
    ]


def _stale_configuration_doc() -> bool:
    spec = importlib.util.spec_from_file_location(
        "gen_config_docs", REPO_ROOT / "scripts" / "gen_config_docs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    on_disk = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    return bool(module.render_markdown() != on_disk)


# ── the rules ────────────────────────────────────────────────────────────────


def evaluate() -> list[tuple[str, list[str]]]:
    registered = concrete_names()
    scanned = scan_env_reads()
    scripts = _embedded_scripts()
    script_names = {label: _script_env_names(text) for label, text in scripts.items()}
    litellm = _bundled_litellm_reads()
    failures: list[tuple[str, list[str]]] = []

    def report(reason: str, items: list[str]) -> None:
        if items:
            failures.append((reason, items))

    report(
        "read under astrabox/ with no registry row. Add a row to "
        "astrabox/config/env_registry.py, then run scripts/gen_config_docs.py",
        [
            f"{name}  ({sorted(scanned.sites[name])[0][0]})"
            for name in sorted(scanned.names - registered)
        ],
    )
    for label, names in script_names.items():
        report(
            f"read by the embedded script {label} with no registry row",
            sorted(names - registered),
        )
    report(
        "read by the bundled gateway or another pinned runtime with no registry row",
        sorted((litellm | _EXTERNAL_RUNTIME_ENV_READS) - registered),
    )
    known = (
        scanned.names
        | set().union(*script_names.values())
        | litellm
        | _EXTERNAL_RUNTIME_ENV_READS
    )
    report(
        "registered and read nowhere. Remove the row, or fix its name if the read moved",
        sorted(registered - known),
    )
    report(
        "read with a name the scan cannot resolve. Make it a module-level constant, "
        "add the wrapper to _WRAPPER_CALLS, or list the site in _ALLOWED_UNRESOLVED "
        "with a pattern row in the registry",
        [f"{module}:{line}" for module, line in sorted(scanned.unresolved - _ALLOWED_UNRESOLVED)],
    )
    report(
        "listed in _ALLOWED_UNRESOLVED and not an unresolved read. Update the list",
        [f"{module}:{line}" for module, line in sorted(_ALLOWED_UNRESOLVED - scanned.unresolved)],
    )
    environments = _compose_server_environments()
    for compose_file, environment in environments.items():
        report(
            f"forwarded to the server by containers/{compose_file} with no registry row",
            sorted(set(environment) - registered),
        )
    base = environments.get("compose.yaml", {})
    report(
        "not forwarded by containers/compose.yaml with the setting's own default",
        [
            f"{name}: expected {expected!r}, found {base.get(name)!r}"
            for name, expected in _COMPOSE_FORWARDS.items()
            if base.get(name) != expected
        ],
    )
    report(
        "declared on AstraBoxRuntimeSettings and read by no module outside the "
        "declaration. Wire each field to its real authority or remove it",
        _unread_runtime_settings(),
    )
    report(
        "copied as a literal in astrabox/deploy/onebox.py and different from its source",
        _diverged_onebox_copies(),
    )
    if _stale_configuration_doc():
        report(
            "out of date. Run `python scripts/gen_config_docs.py` and commit the result",
            ["docs/configuration.md"],
        )
    return failures


def main() -> int:
    failures = evaluate()
    for reason, items in failures:
        print(f"\n{len(items)} item(s) {reason}:")
        for item in items:
            print(f"  {item}")
    registered = len(concrete_names())
    if failures:
        print(f"\nenv registry FAILED — {registered} registered names")
        return 1
    print(f"env registry OK — {registered} registered names, each read and documented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
