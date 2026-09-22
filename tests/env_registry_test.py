"""The env-var registry must stay a complete, fresh census of ``astrabox/``.

Five independent guarantees, each pinned by its own test below:

1. **Completeness** — an AST-based scanner walks every ``.py`` file under
   ``astrabox/`` and finds every environment-variable READ: ``os.getenv`` /
   ``os.environ[...]`` / ``os.environ.get`` with a literal name, the
   ``_env`` / ``_strict_flag`` wrappers in ``providers/identity_sso.py``
   (first arg), and the ``AstraBoxSettings`` + ``AstraBoxRuntimeSettings``
   pydantic fields (a framework-magic read no ``os.getenv``-shaped AST node can
   see — ``common/utils/settings.py``'s wide operational surface is now these
   fields' ``AliasChoices`` — so it is resolved by introspecting the live classes
   instead). Every name the scanner finds must have
   a row in :data:`astrabox.config.env_registry.ENV_REGISTRY`
   (``test_every_scanned_env_read_has_a_registry_entry``) — a new env read with
   no row fails loud instead of silently going undocumented.
2. **Freshness (no stale rows)** — every registry row must match a name the
   AST scanner OR the embedded-script census below still finds
   (``test_registry_entries_are_not_stale``) — a renamed or deleted read site
   leaves its old row behind otherwise.
3. **Docs freshness** — ``docs/configuration.md`` must be exactly what
   ``scripts/gen_config_docs.py`` renders from the CURRENT registry
   (``test_configuration_docs_are_generated_freshly``) — a registry edit with no
   regenerate drifts the committed doc from its source of truth otherwise.
4. **Embedded-script completeness** — the AST scanner above only sees env reads
   in astrabox/'s own parseable Python source; a name read inside a SCRIPT
   STRING LITERAL shipped into the sandbox (a bash heredoc, or a Python shim
   rendered as an f-string) never appears as an ``os.getenv``-shaped AST node,
   so it is invisible to guarantee 1 above. ``test_embedded_script_env_reads_have_a_registry_entry``
   regex-extracts the literal env names present in each enumerated script
   producer and asserts that every name has a registry row —
   see the "Embedded-script census" section below.
5. **Quickstart integrity** — every setting in ``.env.example`` either matches
   its registry default or has one explicit expected value and reason below;
   Compose-only settings have the same explicit treatment. This keeps the
   hand-picked quickstart useful without letting its values silently drift from
   the registry (``test_env_example_values_are_defaults_or_documented_examples``).

A handful of env reads are genuinely dynamic — the argument is not a literal and
cannot be resolved to one by the constant-folding this scanner does (see
``_resolve_name`` / ``ModuleScanner.visit_For``) — but ARE still literal
composition (module-level ``NAME = "LITERAL"`` constants, and small ``for`` loops
over a literal tuple of names). Those are resolved, not exempted. Exactly one
site is truly dynamic — ``astrabox/secrets.py``'s
``SecretProvider.get_secret``, which reads an operator-CHOSEN name
(``secret_name.upper().replace("-", "_")``) — and it is the one entry in
``_ALLOWED_UNRESOLVED`` below, documented in the registry as the
``SECRET_NAME_PATTERN_ROW`` pattern instead of a literal row. A NEW dynamic site
the scanner cannot resolve fails ``test_no_new_unresolved_dynamic_env_reads``
loudly rather than silently vanishing from the census.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ASTRABOX_ROOT = _REPO_ROOT / "astrabox"

from astrabox.config.env_registry import ENV_REGISTRY, concrete_names  # noqa: E402
from astrabox.providers.sandbox_image import IN_BOX_SIDECAR_PORT  # noqa: E402

_ENV_EXAMPLE_NONDEFAULT_VALUES: dict[str, tuple[str, str]] = {
    "ANTHROPIC_API_KEY": (
        "sk-your-key-here",
        "A fill-in placeholder makes the one required operator action visible.",
    ),
    "ASTRABOX_AGENT_PREWARM_REDIS_URL": (
        "redis://127.0.0.1:56379/0",
        "The commented example shows an explicit client-pool coordination URL; "
        "the runtime default is unset and Compose supplies its internal URL.",
    ),
    "ASTRABOX_AWS_KMS_KEY_ARN": (
        "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000",
        "The KMS example uses a structurally valid placeholder key ARN.",
    ),
    "ASTRABOX_DB_URL": (
        "postgresql+asyncpg://astrabox:change-me@db.example.com:5432/astrabox",
        "The quickstart shows the shape required for an external PostgreSQL database.",
    ),
    "ASTRABOX_EXTENSION_PROVIDER": (
        "litellm",
        "The optional provider example names the bundled implementation explicitly.",
    ),
    "ASTRABOX_LITELLM_API_KEY": (
        "sk-astrabox-local",
        "The external-gateway example needs a recognizable placeholder credential.",
    ),
    "ASTRABOX_LITELLM_BASE_URL": (
        "https://gateway.example.com",
        "The optional external-gateway example needs a sandbox-reachable URL.",
    ),
    "ASTRABOX_LITELLM_SERVER_BASE_URL": (
        "http://127.0.0.1:4000",
        "The split-network example shows a distinct server-side gateway URL.",
    ),
    "ASTRABOX_LOCAL_MODE": (
        "1",
        "The commented example demonstrates how to enable this opt-in mode.",
    ),
    "ASTRABOX_MCP_PROXY_BASE_URL": (
        "https://sandbox-callback.example.com",
        "The external callback-route example needs a concrete URL shape.",
    ),
    "ASTRABOX_MODEL_ENDPOINT_PROVIDER": (
        "litellm",
        "The optional gateway example demonstrates explicit provider selection.",
    ),
    "ASTRABOX_MODEL_GATEWAY_REQUIRE_HTTPS": (
        "1",
        "The team-gateway example demonstrates opting into the HTTPS requirement.",
    ),
    "ASTRABOX_PUBLISH_HOST_IP": (
        "172.17.0.1",
        "The maintained Compose stack runs AstraBox in a container and uses the "
        "Docker bridge gateway instead of the host-process default.",
    ),
    "ASTRABOX_SANDBOX_CREDENTIAL_VAULT": (
        "0",
        "The commented example demonstrates explicitly disabling the default vault.",
    ),
    "ASTRABOX_SANDBOX_ENDPOINT_SCHEME": (
        "https",
        "The Kubernetes ingress example uses TLS rather than the unset default.",
    ),
    "ASTRABOX_SANDBOX_SECURE_ACCESS": (
        "true",
        "The Kubernetes ingress example demonstrates enabling signed links.",
    ),
    "ASTRABOX_SANDBOX_SERVER_INGRESS_GATEWAY_ADDRESS": (
        "sandbox.example.com",
        "The Kubernetes ingress example needs a concrete public-hostname shape.",
    ),
    "ASTRABOX_SANDBOX_SERVER_INGRESS_MODE": (
        "gateway",
        "The Kubernetes ingress example selects the non-default gateway mode.",
    ),
    "ASTRABOX_SANDBOX_SERVER_PORT_RANGE": (
        "62000-63000",
        "The shared-Docker example demonstrates choosing a range distinct from the default.",
    ),
    "ASTRABOX_SANDBOX_SERVER_RUNTIME": (
        "kubernetes",
        "The Kubernetes deployment example selects the non-default runtime.",
    ),
    "ASTRABOX_SECRET_STORE": (
        "aws_kms",
        "The multi-replica example selects the non-default KMS provider.",
    ),
    "ASTRABOX_VAULT_MASTER_KEY": (
        "",
        "Operators fill this field; the registry's generated sentinel is not a literal key.",
    ),
}

_ENV_EXAMPLE_COMPOSE_VALUES: dict[str, tuple[str, str]] = {
    "ASTRABOX_POSTGRES_VOLUME": (
        "astrabox-postgres",
        "Compose interpolation names the persistent PostgreSQL volume.",
    ),
    "ASTRABOX_SERVER_IMAGE": (
        "astrabox/server:latest",
        "Compose and the source launcher select the server image before AstraBox starts.",
    ),
    "ASTRABOX_STATE_VOLUME": (
        "astrabox-state",
        "Compose interpolation names the persistent AstraBox state volume.",
    ),
    "LITELLM_DATABASE_URL": (
        "postgresql://litellm:change-me@db.example.com:5432/litellm",
        "Compose maps this launcher-facing value to LiteLLM's DATABASE_URL.",
    ),
}

# ─────────────────────────────────────────────────────────────────────────────
# Wrapper functions that compose an env-var name from a literal first argument
# (grep-evading by construction — a plain ``grep os.getenv`` never sees these
# call sites). Keyed by (defining module, function name); value is the index of
# the literal-name argument at each CALL site.
# ─────────────────────────────────────────────────────────────────────────────
_WRAPPER_CALLS: dict[tuple[str, str], int] = {
    ("astrabox.providers.identity_sso", "_env"): 0,
    ("astrabox.providers.identity_sso", "_strict_flag"): 0,
    ("astrabox.providers.identity_oidc", "_env"): 0,
    ("astrabox.identity.oidc", "_env"): 0,
    ("astrabox.deploy.sandbox_server", "_env"): 0,
    ("astrabox.deploy.sandbox_server", "_flag"): 0,
    ("astrabox.deploy.onebox", "_env"): 0,
    ("astrabox.deploy.onebox", "export_default"): 0,
    (
        "astrabox.core.service.orchestrator.session_kernel.workers.reconcile_worker",
        "_env_float",
    ): 0,
}

#: The wrappers' OWN implementations read `os.getenv(<formal parameter>)` — that
#: internal read is not a new finding (it is already represented by every call
#: site above), so it is skipped rather than reported as unresolved-dynamic.
_WRAPPER_IMPL_FUNCS: dict[str, frozenset[str]] = {
    "astrabox.providers.identity_sso": frozenset({"_env", "_strict_flag"}),
    "astrabox.providers.identity_oidc": frozenset({"_env"}),
    "astrabox.identity.oidc": frozenset({"_env"}),
    "astrabox.deploy.sandbox_server": frozenset({"_env", "_flag"}),
    "astrabox.deploy.onebox": frozenset({"_env", "export_default"}),
    "astrabox.core.service.orchestrator.session_kernel.workers.reconcile_worker": frozenset(
        {"_env_float"}
    ),
}

#: The one genuinely dynamic read site: ``astrabox/secrets.py``'s
#: ``SecretProvider.get_secret`` does
#: ``os.getenv(secret_env_key(secret_name))`` — a CALL expression, not a name a
#: module-constant or for-loop table could ever resolve, because the value comes
#: from a caller-supplied string at runtime. Documented as
#: ``env_registry.SECRET_NAME_PATTERN_ROW`` instead of a literal row.
_ALLOWED_UNRESOLVED: frozenset[tuple[str, int]] = frozenset(
    {
        ("astrabox.secrets", 42),
    }
)


def _module_dotted(path: Path) -> str:
    """``astrabox/foo/bar.py`` (repo-root-relative) -> ``"astrabox.foo.bar"``."""
    rel = path.relative_to(_REPO_ROOT)
    return ".".join(rel.with_suffix("").parts)


def _literal_str(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _collect_module_literals(tree: ast.Module) -> dict[str, Any]:
    """Module-level ``NAME = <literal>`` (and annotated) assignments, evaluated.

    Powers two resolution shapes seen in the tree: a bare module constant used
    as an env-read argument (``DEFAULT_AGENT_IMAGE_ENV = "ASTRABOX_AGENT_IMAGE"``
    then ``os.environ.get(DEFAULT_AGENT_IMAGE_ENV)``), and a for-loop iterating a
    module-level tuple of literal names/pairs (see ``ModuleScanner.visit_For``).
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
class _ScanResult:
    #: name -> {(module, lineno, how), ...}
    sites: dict[str, set[tuple[str, int, str]]] = field(default_factory=dict)
    #: (module, lineno) for a read whose argument could not be resolved to a literal.
    unresolved: set[tuple[str, int]] = field(default_factory=set)

    @property
    def names(self) -> set[str]:
        return set(self.sites)

    def add(self, name: str, module: str, lineno: int, how: str) -> None:
        self.sites.setdefault(name, set()).add((module, lineno, how))


class _ModuleScanner(ast.NodeVisitor):
    """Finds env-var reads in one module's AST (see the file docstring for scope)."""

    def __init__(self, module: str, result: _ScanResult) -> None:
        self.module = module
        self.result = result
        self.module_literals: dict[str, Any] = {}
        self._func_stack: list[str] = []
        self._scope_stack: list[dict[str, frozenset[str]]] = []

    def _in_skipped_wrapper_body(self) -> bool:
        skip = _WRAPPER_IMPL_FUNCS.get(self.module, frozenset())
        return any(f in skip for f in self._func_stack)

    def _resolve_name(self, node: ast.Name) -> list[str] | None:
        for scope in reversed(self._scope_stack):
            if node.id in scope:
                return sorted(scope[node.id])
        value = self.module_literals.get(node.id)
        return [value] if isinstance(value, str) else None

    def _handle(self, node: ast.AST, arg: ast.expr, how: str) -> None:
        if self._in_skipped_wrapper_body():
            return
        literal = _literal_str(arg)
        if literal is not None:
            self.result.add(literal, self.module, node.lineno, how)
            return
        if isinstance(arg, ast.Name):
            resolved = self._resolve_name(arg)
            if resolved is not None:
                for name in resolved:
                    self.result.add(name, self.module, node.lineno, f"{how} (resolved)")
                return
        self.result.unresolved.add((self.module, node.lineno))

    # -- scope plumbing ------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _eval_iter(self, iter_node: ast.expr) -> Any:
        if isinstance(iter_node, ast.Name):
            return self.module_literals.get(iter_node.id)
        try:
            return ast.literal_eval(iter_node)
        except Exception:
            return None

    def visit_For(self, node: ast.For) -> None:
        # ``for key in ("A", "B"):`` or ``for a, b in SOME_MODULE_TUPLE:`` — bind
        # the loop variable(s) to the literal value(s) they range over for the
        # duration of the loop body, so an env-read call inside resolves them.
        binding: dict[str, frozenset[str]] = {}
        values = self._eval_iter(node.iter)
        if isinstance(values, (list, tuple)) and values:
            if isinstance(node.target, ast.Name):
                strs = frozenset(v for v in values if isinstance(v, str))
                if strs:
                    binding[node.target.id] = strs
            elif isinstance(node.target, (ast.Tuple, ast.List)):
                for i, elt in enumerate(node.target.elts):
                    if not isinstance(elt, ast.Name):
                        continue
                    col = frozenset(
                        row[i]
                        for row in values
                        if isinstance(row, (list, tuple))
                        and len(row) > i
                        and isinstance(row[i], str)
                    )
                    if col:
                        binding[elt.id] = col
        if binding:
            self._scope_stack.append(binding)
        self.generic_visit(node)
        if binding:
            self._scope_stack.pop()

    # -- the actual env-read shapes ------------------------------------------

    def visit_Subscript(self, node: ast.Subscript) -> None:
        val = node.value
        if (
            isinstance(val, ast.Attribute)
            and val.attr == "environ"
            and isinstance(val.value, ast.Name)
            and val.value.id == "os"
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
            idx = _WRAPPER_CALLS.get((self.module, func.id))
            if idx is not None and len(node.args) > idx:
                self._handle(node, node.args[idx], func.id)
        self.generic_visit(node)


def _scan_ast() -> _ScanResult:
    result = _ScanResult()
    for path in sorted(_ASTRABOX_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _module_dotted(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scanner = _ModuleScanner(module, result)
        scanner.module_literals = _collect_module_literals(tree)
        scanner.visit(tree)
    return result


def _add_pydantic_settings_fields(result: _ScanResult) -> None:
    """Env names read by ``pydantic-settings`` itself — no ``os.getenv``-shaped AST
    node exists for the scanner to find, so they are resolved by introspecting the
    live ``BaseSettings`` classes (their own alias/prefix rules, not a re-parse of
    how ``Field(...)`` happens to be written).

    Covers both typed-settings surfaces: ``AstraBoxSettings`` (LLM/db/state) and
    ``AstraBoxRuntimeSettings`` (the wide operational config, whose ``ASTRABOX_*``
    names live in each field's ``AliasChoices``). A field with only an
    ``AliasPath`` (a YAML-only field) contributes no env name — exactly matching
    that it reads no environment variable."""
    from pydantic import AliasChoices

    from astrabox.common.utils.settings import AstraBoxRuntimeSettings
    from astrabox.config.settings import AstraBoxSettings

    for settings_cls in (AstraBoxSettings, AstraBoxRuntimeSettings):
        prefix = str(settings_cls.model_config.get("env_prefix") or "")
        module = settings_cls.__module__
        for field_name, info in settings_cls.model_fields.items():
            alias = info.validation_alias
            if isinstance(alias, AliasChoices):
                names = [c for c in alias.choices if isinstance(c, str)]
            elif isinstance(alias, str):
                names = [alias]
            else:
                names = [(prefix + field_name).upper()]
            for name in names:
                result.add(name, module, 0, f"pydantic-field:{field_name}")


@lru_cache(maxsize=1)
def _scan_env_reads() -> _ScanResult:
    # Cached: every test below wants the same whole-tree scan, and re-parsing
    # ~200 files under astrabox/ per test would multiply this file's runtime for
    # no benefit (the scan is a pure function of the checked-out tree).
    result = _scan_ast()
    _add_pydantic_settings_fields(result)
    return result


def _load_gen_config_docs() -> Any:
    spec = importlib.util.spec_from_file_location(
        "gen_config_docs", _REPO_ROOT / "scripts" / "gen_config_docs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── embedded-script environment census ──────────────────────────────────────
# The AST scanner cannot inspect environment reads inside generated shell or
# Python source strings. These runtime assets require explicit string scanning:
#
#   * ``hermes._HERMES_PROFILE_SETUP_SCRIPT`` — a bash+python heredoc script
#     installed into the sandbox to prepare the per-profile Hermes files.
#   * ``runtime/astrabox-assistant-workspace-storage`` — the image-owned shared
#     workspace mount helper.
#   * ``runtime/provision-conversation`` — the image-owned conversation
#     bootstrap and its command-scoped host-to-sandbox environment.
#   * ``runtime/astrabox-transcript-mirror`` — the image-owned relay that
#     mirrors an engine's own session log out of a box. Both engine images
#     install the same program and tell it, through the environment, which
#     files theirs writes.
#   * ``sandbox-hermes/astrabox-hermes-serve`` — the image-owned launcher
#     that reads the Assistant profile and WebSocket session credential.
#   * ``sandbox-hermes/astrabox-hermes-forward`` — its readiness-gated proxy.
#
# Extract every literal name and require a registry row. Add any new embedded
# script producer to this census explicitly.

_ENV_NAME_SHAPE = r"[A-Z][A-Z0-9_]*"

# os.environ.get("X") / os.environ.get('X')
_RE_ENVIRON_GET = re.compile(rf"os\.environ\.get\(\s*[\"']({_ENV_NAME_SHAPE})[\"']")
# os.environ["X"] / os.environ['X']
_RE_ENVIRON_GETITEM = re.compile(rf"os\.environ\[\s*[\"']({_ENV_NAME_SHAPE})[\"']\s*\]")
# "X" in os.environ / 'X' in os.environ
_RE_ENVIRON_CONTAINS = re.compile(rf"[\"']({_ENV_NAME_SHAPE})[\"']\s+in\s+os\.environ")
# a `keys = [...]` / `keys += [...]` python list literal of bare quoted names
# (the gateway script's own env-forwarding allowlist) — scoped to the bracketed
# block (non-greedy up to the first `]`) so an unrelated quoted string
# elsewhere in the script (a printf format string, a heredoc delimiter like
# ``<<'PY'``) is never mistaken for a name: neither is a bare `keys = [...]`
# list element, so scoping this way finds only real candidates.
_RE_KEYS_LIST_BLOCK = re.compile(r"\bkeys\s*\+?=\s*\[(.*?)\]", re.DOTALL)
_RE_QUOTED_NAME = re.compile(rf"[\"']({_ENV_NAME_SHAPE})[\"']")
# bash ${NAME} / ${NAME:-default} reads — scoped to the namespaces these
# scripts actually use, so a local lowercase shell var (``${workspace}``, the
# gateway script's own ``required()`` helper's ``${!name:-}`` indirection)
# never matches.
_RE_BASH_BRACE = re.compile(
    r"\$\{(ASTRABOX_[A-Z0-9_]+|HERMES_[A-Z0-9_]+|API_SERVER_[A-Z0-9_]+|"
    r"CONV_[A-Z0-9_]+|GIT_HTTPS_TOKEN)(?::[^}]*)?\}"
)
# bash `$(required NAME)` reads — the gateway script's fail-loud accessor takes
# the env NAME as a bare function argument, so neither the ${...} nor the
# quoted-name families see it. Same namespaces as _RE_BASH_BRACE.
_RE_BASH_REQUIRED = re.compile(
    r"\b(?:required|require_env)\s+"
    r"(ASTRABOX_[A-Z0-9_]+|HERMES_[A-Z0-9_]+|API_SERVER_[A-Z0-9_]+|"
    r"CONV_[A-Z0-9_]+|GIT_HTTPS_TOKEN)\b"
)

#: Embedded-script reads intentionally excluded from the registry. Each entry
#: requires a comment explaining why it is not a configuration surface.
_EMBEDDED_SCRIPT_CENSUS_EXCLUDED: frozenset[str] = frozenset()

# Pinned third-party processes read these names. Hermes receives its custom
# provider key in a private profile env file; LiteLLM's Langfuse callback reads
# the three observability values; the pi image's boot script renders the
# vendor's models.json from its base URL and model, and pi itself resolves the
# credential out of the environment at request time (models.json carries the
# variable's NAME, so the value never lands on disk). No local ``os.environ``
# expression can appear in the AST census for reads performed inside a sandbox
# image or by an installed package.
_EXTERNAL_RUNTIME_ENV_READS: frozenset[str] = frozenset(
    {
        "ASTRABOX_HERMES_MODEL_API_KEY",
        "ASTRABOX_PI_API_KEY",
        "ASTRABOX_PI_BASE_URL",
        "ASTRABOX_PI_MODEL",
        "LANGFUSE_HOST",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    }
)


def _bundled_litellm_env_reads() -> frozenset[str]:
    config = (_REPO_ROOT / "containers" / "litellm" / "config.yaml").read_text(
        encoding="utf-8"
    )
    return frozenset(re.findall(r"\bos\.environ/([A-Z][A-Z0-9_]*)\b", config))


def _extract_embedded_script_env_names(script: str) -> set[str]:
    names: set[str] = set()
    for pattern in (
        _RE_ENVIRON_GET,
        _RE_ENVIRON_GETITEM,
        _RE_ENVIRON_CONTAINS,
        _RE_BASH_BRACE,
        _RE_BASH_REQUIRED,
    ):
        names.update(m.group(1) for m in pattern.finditer(script))
    for block in _RE_KEYS_LIST_BLOCK.finditer(script):
        names.update(m.group(1) for m in _RE_QUOTED_NAME.finditer(block.group(1)))
    return names


def _embedded_script_env_names() -> frozenset[str]:
    """Every literal env name found across the embedded-script census.

    Keeps ``test_registry_entries_are_not_stale`` from flagging a registry
    row that is real but only visible here, not to the AST scanner (which
    cannot see inside a script string literal at all).
    """
    names: set[str] = set()
    for script in _embedded_scripts().values():
        names.update(_extract_embedded_script_env_names(script))
    return frozenset(names)


def _embedded_scripts() -> dict[str, str]:
    from astrabox.core.service.orchestrator.engine.hermes import (
        _HERMES_PROFILE_SETUP_SCRIPT,
    )
    return {
        "hermes._HERMES_PROFILE_SETUP_SCRIPT": _HERMES_PROFILE_SETUP_SCRIPT,
        "runtime/astrabox-assistant-workspace-storage": (
            _REPO_ROOT
            / "astrabox/core/service/orchestrator/runtime/astrabox-assistant-workspace-storage"
        ).read_text(encoding="utf-8"),
        "runtime/provision-conversation": (
            _REPO_ROOT
            / "astrabox/core/service/orchestrator/runtime/provision-conversation"
        ).read_text(encoding="utf-8"),
        "runtime/astrabox-transcript-mirror": (
            _REPO_ROOT / "astrabox/core/service/orchestrator/runtime/astrabox-transcript-mirror"
        ).read_text(encoding="utf-8"),
        "sandbox-hermes/astrabox-hermes-serve": (
            _REPO_ROOT / "containers/sandbox-hermes/astrabox-hermes-serve"
        ).read_text(encoding="utf-8"),
        "sandbox-hermes/astrabox-hermes-forward": (
            _REPO_ROOT / "containers/sandbox-hermes/astrabox-hermes-forward"
        ).read_text(encoding="utf-8"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_every_scanned_env_read_has_a_registry_entry() -> None:
    scanned = _scan_env_reads()
    missing = sorted(scanned.names - concrete_names())
    assert not missing, (
        "env var(s) read somewhere in astrabox/ with no astrabox/config/env_registry.py "
        f"row: {missing}. Add a (name, tier, default, description, read_site) row for each, "
        "then `python scripts/gen_config_docs.py`."
    )


def test_registry_entries_are_not_stale() -> None:
    scanned = _scan_env_reads()
    known = (
        scanned.names
        | _embedded_script_env_names()
        | _bundled_litellm_env_reads()
        | _EXTERNAL_RUNTIME_ENV_READS
    )
    stale = sorted(concrete_names() - known)
    assert not stale, (
        f"astrabox/config/env_registry.py row(s) with no matching env read left in "
        f"astrabox/ (AST scan) or the embedded-script census: {stale}. Remove the row "
        "(or fix its name) if the read site was renamed or removed."
    )


def test_no_new_unresolved_dynamic_env_reads() -> None:
    scanned = _scan_env_reads()
    unexpected = sorted(scanned.unresolved - _ALLOWED_UNRESOLVED)
    assert not unexpected, (
        f"new dynamic (non-literal) env-read site(s) the scanner cannot resolve to a "
        f"literal name: {unexpected}. Either make it a resolvable composition (a "
        'module-level NAME = "LITERAL" constant, or register it in _WRAPPER_CALLS) or '
        "add it to _ALLOWED_UNRESOLVED with a registry pattern row explaining why it is "
        "genuinely dynamic (see SECRET_NAME_PATTERN_ROW)."
    )
    also_missing = sorted(_ALLOWED_UNRESOLVED - scanned.unresolved)
    assert not also_missing, (
        f"_ALLOWED_UNRESOLVED entries no longer found as unresolved by the scanner "
        f"(the read site moved, was resolved, or was removed): {also_missing}. Update "
        "_ALLOWED_UNRESOLVED to match."
    )


def test_embedded_script_env_reads_have_a_registry_entry() -> None:
    registry_names = concrete_names()
    for label, script in _embedded_scripts().items():
        found = _extract_embedded_script_env_names(script) - _EMBEDDED_SCRIPT_CENSUS_EXCLUDED
        missing = sorted(found - registry_names)
        assert not missing, (
            f"{label} reads env var(s) with no astrabox/config/env_registry.py row: "
            f"{missing}. Add a (name, tier='injected', default, description, read_site) "
            "row for each, then `python scripts/gen_config_docs.py`."
        )


def test_bundled_litellm_env_reads_have_registry_entries() -> None:
    missing = sorted(
        (_bundled_litellm_env_reads() | _EXTERNAL_RUNTIME_ENV_READS)
        - concrete_names()
    )
    assert not missing, (
        "environment variables consumed by bundled third-party runtimes need "
        f"env_registry.py rows too: {missing}"
    )


def test_compose_server_environment_is_governed_by_the_registry() -> None:
    class ComposeLoader(yaml.SafeLoader):
        pass

    def construct_override(loader: yaml.SafeLoader, node: yaml.Node) -> object:
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        return loader.construct_scalar(node)

    ComposeLoader.add_constructor("!override", construct_override)
    ComposeLoader.add_constructor("!reset", lambda loader, node: None)
    forwarded: set[str] = set()
    for path in (_REPO_ROOT / "containers").glob("compose*.yaml"):
        compose = yaml.load(path.read_text(encoding="utf-8"), Loader=ComposeLoader)
        environment = compose.get("services", {}).get("server", {}).get("environment", {})
        forwarded.update(environment or {})

    assert not forwarded - concrete_names(), (
        "a maintained server Compose overlay forwards unregistered or inert "
        f"environment variables: {sorted(forwarded - concrete_names())}"
    )


def test_configuration_docs_are_generated_freshly() -> None:
    gen = _load_gen_config_docs()
    generated = gen.render_markdown()
    doc_path = _REPO_ROOT / "docs" / "configuration.md"
    on_disk = doc_path.read_text(encoding="utf-8")
    assert generated == on_disk, (
        "docs/configuration.md does not match astrabox/config/env_registry.py — run "
        "`python scripts/gen_config_docs.py` and commit the diff."
    )


def test_runner_port_matches_the_image_contract_everywhere() -> None:
    """Pooled and resumed boxes must listen where the host dials."""
    boot_script = (
        _REPO_ROOT / "containers" / "sandbox-claude-code" / "boot.sh"
    ).read_text(encoding="utf-8")
    boot_default_match = re.search(
        r'^: "\$\{ASTRABOX_RUNNER_PORT:=(\d+)\}"$', boot_script, re.MULTILINE
    )
    assert boot_default_match is not None, (
        "boot.sh must declare the runner's default port so image-only startup "
        "works for pooled and resumed boxes"
    )
    registry_default = next(
        row.default for row in ENV_REGISTRY if row.name == "ASTRABOX_RUNNER_PORT"
    )

    assert (
        int(boot_default_match.group(1))
        == IN_BOX_SIDECAR_PORT
        == int(registry_default)
    ), (
        "boot.sh chooses where pooled and resumed boxes listen, sandbox_image.py "
        "chooses where the host dials, and env_registry.py publishes that contract; "
        "the three values must move together"
    )


def test_env_example_values_are_defaults_or_documented_examples() -> None:
    """The hand-picked quickstart must not silently drift from the registry."""
    text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    pairs = re.findall(r"^(?:#\s*)?([A-Z][A-Z0-9_]*)=(.*)$", text, re.MULTILINE)
    assignments = dict(pairs)
    assert len(assignments) == len(pairs), (
        ".env.example defines the same setting more than once; keep one operator "
        "example per name so its intended value is unambiguous."
    )

    registry = {row.name: row.default for row in ENV_REGISTRY if not row.is_pattern}
    compose_values = {
        name: value for name, (value, _reason) in _ENV_EXAMPLE_COMPOSE_VALUES.items()
    }
    unregistered = set(assignments) - set(registry)
    assert unregistered == set(compose_values), (
        ".env.example settings outside the application registry must be explicit "
        "Compose/launcher inputs with a reason; unexpected or stale names: "
        f"{sorted(unregistered ^ set(compose_values))}"
    )
    actual_compose_values = {name: assignments[name] for name in unregistered}
    compose_mismatches = sorted(
        name
        for name in set(actual_compose_values) | set(compose_values)
        if actual_compose_values.get(name) != compose_values.get(name)
    )
    assert not compose_mismatches, (
        ".env.example Compose/launcher examples changed without updating their "
        f"documented expected values: {compose_mismatches}"
    )

    nondefault_values = {
        name: value
        for name, value in assignments.items()
        if name in registry and value != registry[name]
    }
    documented_nondefaults = {
        name: value
        for name, (value, _reason) in _ENV_EXAMPLE_NONDEFAULT_VALUES.items()
    }
    nondefault_mismatches = sorted(
        name
        for name in set(nondefault_values) | set(documented_nondefaults)
        if nondefault_values.get(name) != documented_nondefaults.get(name)
    )
    assert not nondefault_mismatches, (
        ".env.example registry-backed values must match their defaults unless an "
        "exact example value and reason are documented; unexpected or stale names: "
        f"{nondefault_mismatches}"
    )

    missing_reasons = sorted(
        name
        for examples in (_ENV_EXAMPLE_NONDEFAULT_VALUES, _ENV_EXAMPLE_COMPOSE_VALUES)
        for name, (_value, reason) in examples.items()
        if not reason.strip()
    )
    assert not missing_reasons, f".env.example allowlist entries need reasons: {missing_reasons}"


@pytest.mark.parametrize("name", sorted(concrete_names()))
def test_registry_name_looks_like_an_env_var(name: str) -> None:
    """Cheap shape guard: every concrete name is upper-snake, no stray whitespace."""
    assert name == name.strip()
    assert name == name.upper()
    assert " " not in name
