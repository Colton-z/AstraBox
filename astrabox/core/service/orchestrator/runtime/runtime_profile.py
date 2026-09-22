"""Select an engine's filesystem declaration and plan installed capabilities."""

from __future__ import annotations

import hashlib
import json
import posixpath
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.capabilities import (
    EngineRuntimeProfileDeclaration,
    unsupported_engine_configuration_inputs,
)
from astrabox.core.service.orchestrator.runtime.plugin_repos import (
    build_plugin_repo_checkout_dir,
    get_template_plugin_repos,
)
from astrabox.seams.sandbox import (
    SANDBOX_PERMISSION_LEVEL_DEFAULT,
    SANDBOX_PERMISSION_LEVELS,
    SANDBOX_TENANCIES,
    SANDBOX_TENANCY_AGENT,
    SANDBOX_TENANCY_CONVERSATION,
)


def resolve_sandbox_permission_level(template: Any | None) -> str:
    """What a box of this environment is granted.

    Unset means the substrate's minimum: a level is something a deployment asks
    for, never something it inherits by omission. An unrecognised value is
    refused rather than treated as the minimum, because silently downgrading a
    request for containment is the failure this vocabulary exists to make
    visible.
    """
    value = str(_template_value(template, "sandbox_permission_level") or "").strip().lower()
    if not value:
        return SANDBOX_PERMISSION_LEVEL_DEFAULT
    if value not in SANDBOX_PERMISSION_LEVELS:
        raise APIError(
            code="UNSUPPORTED_SANDBOX_PERMISSION_LEVEL",
            message=(
                f"sandbox_permission_level={value!r} is not one of "
                f"{', '.join(repr(level) for level in SANDBOX_PERMISSION_LEVELS)}"
            ),
            status_code=409,
            data={"sandbox_permission_level": value},
        )
    return value


def resolve_sandbox_tenancy(template: Any | None) -> str:
    """How many conversations a box of this environment carries.

    The form, resolver, and session description use this shared interpretation.
    An unset value means ``conversation`` so mutually untrusted conversations
    remain isolated even when the substrate can host more than one.
    """
    value = str(_template_value(template, "sandbox_tenancy") or "").strip().lower()
    if not value:
        return SANDBOX_TENANCY_CONVERSATION
    if value not in SANDBOX_TENANCIES:
        raise APIError(
            code="UNSUPPORTED_SANDBOX_TENANCY",
            message=(
                f"sandbox_tenancy={value!r} is not one of "
                f"{', '.join(repr(t) for t in SANDBOX_TENANCIES)}"
            ),
            status_code=409,
            data={"sandbox_tenancy": value},
        )
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def resolve_runtime_profile(
    template: Any | None,
    *,
    session_kind: str = "agent_chat",
    engine_kind: str | None = None,
) -> EngineRuntimeProfileDeclaration:
    """Compose the platform's runtime identity for this sandbox tenancy.

    The environment says how many conversations a box carries
    (``sandbox_tenancy`` — a fact about boxes, so it is not the engine's to
    declare), and the platform composes what one conversation looks like at
    that tenancy from its own rules plus the engine's declared facts. There is
    no "engine did not register this tenancy" refusal any more: the substrate
    mechanism behind the shared tenancy is OpenSandbox isolated sessions,
    which any Linux process can inhabit, and whether a given BOX can serve it
    is the box's own answer at claim time (``isolation.capabilities()``).

    The one refusal left is about the engine's INTEGRATION, not the engine:
    an adapter that still drives a single box-scoped service running as the
    image account cannot honour the shared mode's isolation contract
    (separate Linux users, workspaces, isolated sessions) until that service
    is instantiated per conversation. Refusing loudly here is what keeps that
    a visible pending refactor rather than a silent downgrade an operator
    discovers from a support ticket.
    """
    from astrabox.core.service.orchestrator.engine.capabilities import (
        CONVERSATION_PLACEMENT_BOX_ACCOUNT,
        capabilities_for_engine_kind,
        require_engine_for_session_kind,
    )
    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        composed_runtime_profile,
    )

    selected_engine = str(engine_kind or _template_value(template, "engine_kind") or "").strip()
    try:
        selected_engine = require_engine_for_session_kind(selected_engine, session_kind)
    except (KeyError, ValueError) as exc:
        raise APIError(
            code="UNSUPPORTED_RUNTIME_PROFILE",
            message=str(exc),
            status_code=409,
            data={"engine_kind": selected_engine, "session_kind": session_kind},
        ) from exc
    tenancy = resolve_sandbox_tenancy(template)
    if session_kind == "assistant_chat" and tenancy != SANDBOX_TENANCY_AGENT:
        # A product rule, not an engine one: the Assistant product runs on the
        # persistent Assistant-workspace lifecycle, which lives in the shared
        # tenancy's per-account workspace. A box per conversation would come up
        # clean each time and silently abandon that workspace — the failure
        # would read as an Assistant that forgot everything. The old seam got
        # this refusal by accident (the engine had only declared one profile);
        # it is deliberate now, keyed on the session kind the platform owns.
        raise APIError(
            code="UNSUPPORTED_SANDBOX_TENANCY",
            message=(
                "assistant_chat sessions run on the Assistant workspace "
                "lifecycle — a persistent per-account workspace on the shared "
                f"tenancy. sandbox_tenancy={tenancy!r} would provision a box "
                "per conversation and silently abandon that workspace."
            ),
            status_code=409,
            data={"sandbox_tenancy": tenancy, "session_kind": session_kind},
        )
    placement = capabilities_for_engine_kind(selected_engine).conversation_placement
    if (
        tenancy == SANDBOX_TENANCY_AGENT
        and placement == CONVERSATION_PLACEMENT_BOX_ACCOUNT
    ):
        raise APIError(
            code="UNSUPPORTED_SANDBOX_TENANCY",
            message=(
                f"engine {selected_engine!r} cannot serve sandbox_tenancy="
                "'agent' yet: its conversation service is not instantiated "
                "per conversation — the integration drives one box-scoped "
                "service on the image account, which cannot honour the shared "
                "mode's per-conversation isolation. This is a pending "
                "refactor of the integration, not an engine capability."
            ),
            status_code=409,
            data={
                "sandbox_tenancy": tenancy,
                "engine_kind": selected_engine,
                "conversation_placement": placement,
            },
        )
    return composed_runtime_profile(
        selected_engine, tenancy, session_kind=session_kind
    )


def capability_plan_hash(capability_plan: dict[str, Any]) -> str:
    normalized = dict(capability_plan or {})
    normalized.pop("plan_hash", None)
    return sha256_json(normalized)


def template_capability_hash(template: Any | None) -> str:
    """Hash resolved Agent fields that change runtime capabilities."""
    payload = {
        "skills": _template_value(template, "skills") or [],
        "plugin_repos": get_template_plugin_repos(template),
        "mcp_config": _template_value(template, "mcp_servers") or {},
        "default_repo": _template_value(template, "default_repo") or {},
        "system": _template_value(template, "system") or "",
        "engine_options": _template_value(template, "engine_options") or {},
    }
    return sha256_json(payload)


async def probe_sandbox_runtime_profile(
    sandbox: Any,
    profile: EngineRuntimeProfileDeclaration,
) -> dict[str, Any]:
    """Probe image primitives required by the runtime profile."""
    commands = getattr(sandbox, "commands", None)
    run_fn = getattr(commands, "run", None) if commands is not None else None
    if not callable(run_fn):
        raise APIError(
            code="UNSUPPORTED_RUNTIME_PROFILE",
            message="sandbox command runner is required for runtime profile probe",
            status_code=502,
        )

    required = list(profile.required_commands)
    script = "set -e; "
    script += "missing=''; "
    if required:
        script += "for cmd in " + " ".join(_shell_word(item) for item in required) + "; do "
        script += "command -v \"$cmd\" >/dev/null 2>&1 || missing=\"$missing $cmd\"; "
        script += "done; "
    script += "if [ -n \"$missing\" ]; then echo RUNTIME_PROFILE_PROBE_MISSING:$missing; exit 42; fi; "
    script += "echo RUNTIME_PROFILE_PROBE_OK"
    result = await run_fn(f"bash -lc {_shell_word(script)}")
    output = _command_output(result)
    if getattr(result, "error", None) or "RUNTIME_PROFILE_PROBE_OK" not in output:
        missing = []
        marker = "RUNTIME_PROFILE_PROBE_MISSING:"
        if marker in output:
            missing = [item for item in output.split(marker, 1)[1].split() if item]
        raise APIError(
            code="UNSUPPORTED_RUNTIME_PROFILE",
            message=f"runtime profile probe failed: missing_commands={missing or '<unknown>'}; output={output[:1000]!r}",
            status_code=409,
            data={"missing_commands": missing},
        )
    return {
        "sandbox_tenancy": profile.sandbox_tenancy,
        "required_commands": required,
        "missing_commands": [],
        "status": "supported",
    }


def plan_capabilities(template: Any | None, identity: dict[str, Any]) -> dict[str, Any]:
    """Build a conversation-scoped capability plan."""
    config_dir = str(identity.get("config_dir") or "").rstrip("/")
    workspace_dir = str(identity.get("workspace_dir") or "").rstrip("/")
    home_dir = str(identity.get("home_dir") or "").rstrip("/")
    if not workspace_dir or not home_dir:
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message="identity is missing capability planning paths",
            status_code=500,
        )

    skills = [str(item).strip() for item in (_template_value(template, "skills") or []) if str(item).strip()]
    plugin_repos = get_template_plugin_repos(template)
    mcp_servers_map = _template_value(template, "mcp_servers") or {}
    default_repo = _template_value(template, "default_repo")
    from astrabox.core.service.orchestrator.runtime.mcp_servers import (
        template_mcp_servers,
    )

    mcp_servers = template_mcp_servers(mcp_servers_map)
    engine_kind = str(_template_value(template, "engine_kind") or "").strip()
    if not engine_kind and (skills or plugin_repos or mcp_servers):
        raise APIError(
            code="ENGINE_CAPABILITY_UNAVAILABLE",
            message="engine identity is required to plan configured runtime inputs",
            status_code=409,
        )
    unsupported_inputs = (
        unsupported_engine_configuration_inputs(
            engine_kind,
            {
                "mcp_servers": mcp_servers,
                "skills": skills,
                "plugin_repos": plugin_repos,
            },
        )
        if engine_kind
        else ()
    )
    if unsupported_inputs:
        raise APIError(
            code="ENGINE_CAPABILITY_UNAVAILABLE",
            message=(
                f"engine {engine_kind!r} does not consume configured runtime "
                f"fields: {', '.join(unsupported_inputs)}"
            ),
            status_code=409,
        )
    if not config_dir and (skills or plugin_repos or mcp_servers):
        raise APIError(
            code="ENGINE_CAPABILITY_UNAVAILABLE",
            message=(
                "this engine declares no configuration directory, so it cannot "
                "materialize Agent skills, plugins, or MCP configuration"
            ),
            status_code=409,
        )
    plugin_base_dir = f"{config_dir}/plugins" if config_dir else ""
    credential_items: list[dict[str, Any]] = []
    if isinstance(default_repo, dict) and str(default_repo.get("deploy_key_secret_name") or "").strip():
        credential_items.append(
            {
                "kind": "credential",
                "source": "default_repo.deploy_key_secret_name",
                "target_path": f"{home_dir}/.ssh/id_ed25519",
                "execute_as": "sidecar_broker",
                "ownership_repair": "required",
            }
        )
    for index, repo in enumerate(plugin_repos):
        if str(repo.get("deploy_key_secret_name") or "").strip():
            credential_items.append(
                {
                    "kind": "credential",
                    "source": f"plugin_repos[{index}].deploy_key_secret_name",
                    "target_path": f"{home_dir}/.ssh/id_ed25519",
                    "execute_as": "sidecar_broker",
                    "ownership_repair": "required",
                }
            )

    plan = {
        "skill_install_plan": [
            {
                "kind": "skill",
                "name": skill,
                "execute_as": "workload_user",
                "target_path": f"{config_dir}/skills",
                "ownership_repair": "not_required",
            }
            for skill in skills
        ],
        "plugin_repo_plan": [
            {
                "kind": "plugin_repo",
                "index": index,
                "url": repo.get("url"),
                "execute_as": "workload_user",
                "target_path": build_plugin_repo_checkout_dir(
                    str(identity.get("session_id") or ""),
                    index,
                    repo,
                    base_dir=plugin_base_dir,
                ),
                "ownership_repair": "required",
            }
            for index, repo in enumerate(plugin_repos)
        ],
        "mcp_plan": _mcp_plan(mcp_servers, config_dir),
        "default_repo_plan": (
            [
                {
                    "kind": "default_repo",
                    "execute_as": "workload_user",
                    "target_path": workspace_dir,
                    "ownership_repair": "required",
                }
            ]
            if isinstance(default_repo, dict) and str(default_repo.get("url") or "").strip()
            else []
        ),
        "credential_plan": credential_items,
        "debug_log_plan": [
            {
                "kind": "debug_log",
                "execute_as": "workload_user",
                "target_path": f"{config_dir}/debug",
                "ownership_repair": "not_required",
            }
        ] if config_dir else [],
        "history_plan": [
            {
                "kind": "history",
                "execute_as": "workload_user",
                "target_path": config_dir,
                "ownership_repair": "not_required",
            }
        ] if config_dir else [],
    }
    plan["plan_hash"] = capability_plan_hash(plan)
    return plan


def assert_identity_boundary_complete(
    identity: dict[str, Any],
    capability_plan: dict[str, Any],
    *,
    extra_allowed_roots: tuple[str, ...] = (),
) -> None:
    """Reject known shared-root paths for a shared-agent conversation.

    ``workspace_dir`` and ``file_root_dir`` are paths inside the workload's
    namespace. Their box-level backing is recorded separately in
    ``workspace_source_dir``/``file_root_source_dir``. Shared tenancy keeps the
    backing below the conversation home; per-conversation tenancy may use the
    sandbox's image-level workspace directly.
    """
    home = str(identity.get("home_dir") or "")
    allowed_roots = tuple(
        _safe_norm_path(str(root or ""))
        for root in extra_allowed_roots
        if str(root or "").strip()
    )
    visible_keys = {"workspace_dir", "file_root_dir"}
    source_keys = {"workspace_source_dir", "file_root_source_dir"}
    sandbox_tenancy = str(identity.get("sandbox_tenancy") or "").strip()
    required_paths = (
        "home_dir",
        "workspace_dir",
        "workspace_source_dir",
        "file_root_dir",
        "file_root_source_dir",
        "cache_dir",
        "temp_dir",
    )
    for key in (*required_paths, "config_dir"):
        value = str(identity.get(key) or "").strip()
        if key == "config_dir" and not value:
            continue
        if not value:
            raise APIError(
                code="IDENTITY_BOUNDARY_INCOMPLETE",
                message=f"runtime_identity.{key} is empty",
                status_code=500,
            )
        safe_value = _safe_norm_path(value)
        if not safe_value:
            raise APIError(
                code="IDENTITY_BOUNDARY_INCOMPLETE",
                message=f"runtime_identity.{key} is not a safe absolute path: {value}",
                status_code=500,
            )
        if (
            sandbox_tenancy != SANDBOX_TENANCY_CONVERSATION
            and (safe_value == "/root" or safe_value.startswith("/root/"))
        ):
            raise APIError(
                code="IDENTITY_BOUNDARY_INCOMPLETE",
                message=f"runtime_identity.{key} points to root path: {safe_value}",
                status_code=500,
            )
        if key != "home_dir" and home and not _is_under(safe_value, home):
            allowed = key in visible_keys or (
                key in source_keys
                and (
                    sandbox_tenancy == SANDBOX_TENANCY_CONVERSATION
                    or any(
                        root and _is_under(safe_value, root)
                        for root in allowed_roots
                    )
                )
            )
            if not allowed:
                raise APIError(
                    code="IDENTITY_BOUNDARY_INCOMPLETE",
                    message=f"runtime_identity.{key} is outside identity home: {safe_value}",
                    status_code=500,
                )
    for section, items in capability_plan.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            target = str(item.get("target_path") or "").strip()
            safe_target = _safe_norm_path(target) if target else ""
            if target and not safe_target:
                raise APIError(
                    code="IDENTITY_BOUNDARY_INCOMPLETE",
                    message=f"capability {section} target is not a safe absolute path: {target}",
                    status_code=500,
                )
            if safe_target and (
                (
                    sandbox_tenancy != SANDBOX_TENANCY_CONVERSATION
                    and (safe_target == "/root" or safe_target.startswith("/root/"))
                )
                or safe_target.startswith("/usr/local/bin")
            ):
                raise APIError(
                    code="IDENTITY_BOUNDARY_INCOMPLETE",
                    message=f"capability {section} target is not conversation scoped: {safe_target}",
                    status_code=500,
                )
            if safe_target and home and not _is_under(safe_target, home):
                visible_workspace = _safe_norm_path(
                    str(identity.get("workspace_dir") or "")
                )
                if not (
                    item.get("kind") == "default_repo"
                    and visible_workspace
                    and _is_under(safe_target, visible_workspace)
                ):
                    raise APIError(
                        code="IDENTITY_BOUNDARY_INCOMPLETE",
                        message=f"capability {section} target is outside identity home: {safe_target}",
                        status_code=500,
                    )


def validate_workload_identity_evidence(
    initialization: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    """Validate real Claude subprocess identity evidence reported by the sidecar."""
    evidence = (initialization or {}).get("runtime_identity_evidence")
    if not isinstance(evidence, dict):
        raise APIError(
            code="IDENTITY_WORKLOAD_UNVERIFIED",
            message="agent conversation initialization is missing workload identity evidence",
            status_code=502,
        )

    expected_user = str(identity.get("linux_user") or "").strip()
    expected_home = str(identity.get("home_dir") or "").rstrip("/")
    expected_workspace = str(identity.get("workspace_dir") or "").rstrip("/")
    expected_config = str(identity.get("config_dir") or "").rstrip("/")
    expected_bin = f"{expected_home}/.local/bin"

    _require_evidence_equal(evidence, "options_user", expected_user)
    _require_evidence_equal(evidence, "username", expected_user)
    _require_evidence_equal(evidence, "home", expected_home)
    _require_evidence_equal(evidence, "pwd", expected_workspace)
    _require_evidence_equal(evidence, "cwd", expected_workspace)
    _require_evidence_equal(evidence, "options_cwd", expected_workspace)
    _require_evidence_equal(evidence, "config_dir", expected_config)

    expected_uid = _optional_int(identity.get("uid"))
    if expected_uid is not None:
        _require_evidence_int_equal(evidence, "uid", expected_uid)
    path = str(evidence.get("path") or "")
    if not (path == expected_bin or path.startswith(f"{expected_bin}:")):
        raise _workload_mismatch("PATH", path, f"{expected_bin}:...")

    if not bool(evidence.get("config_dir_exists")):
        raise _workload_mismatch("config_dir_exists", evidence.get("config_dir_exists"), True)
    if not bool(evidence.get("config_debug_dir_exists")):
        raise _workload_mismatch("config_debug_dir_exists", evidence.get("config_debug_dir_exists"), True)

    debug_file = str(evidence.get("debug_file") or "").strip()
    if debug_file and not _is_under(debug_file, f"{expected_config}/debug"):
        raise _workload_mismatch("debug_file", debug_file, f"{expected_config}/debug")

    for path_value in evidence.get("plugin_paths") or []:
        path_text = str(path_value or "").strip()
        if path_text and not _is_under(path_text, f"{expected_config}/plugins"):
            raise _workload_mismatch("plugin_path", path_text, f"{expected_config}/plugins")
    expected_plugins = []
    capability_plan = identity.get("capability_plan") if isinstance(identity, dict) else None
    if isinstance(capability_plan, dict):
        expected_plugins = [
            str(item.get("path") or "").strip()
            for item in capability_plan.get("claude_plugin_options") or []
            if isinstance(item, dict) and str(item.get("path") or "").strip()
        ]
    reported_plugins = {
        str(item or "").strip()
        for item in evidence.get("plugin_paths") or []
        if str(item or "").strip()
    }
    for expected_plugin in expected_plugins:
        if expected_plugin not in reported_plugins:
            raise _workload_mismatch("plugin_path", "<missing>", expected_plugin)

    for path_value in evidence.get("add_dirs") or []:
        path_text = str(path_value or "").strip()
        if path_text and not _is_under(path_text, expected_home):
            raise _workload_mismatch("add_dir", path_text, expected_home)

    return evidence


def _mcp_plan(servers: dict[str, Any], config_dir: str) -> list[dict[str, Any]]:
    if not servers:
        return []
    return [
        {
            "kind": "mcp",
            "server_name": str(name),
            "execute_as": "workload_user",
            "target_path": f"{config_dir}/mcp/{name}",
            "ownership_repair": "not_required",
        }
        for name in servers
        if str(name or "").strip()
    ]


def _template_value(template: Any | None, field: str) -> Any:
    if template is None:
        return None
    if isinstance(template, dict):
        return template.get(field)
    return getattr(template, field, None)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_under(path: str, root: str) -> bool:
    value = _safe_norm_path(path)
    base = _safe_norm_path(root)
    return bool(value and base and (value == base or value.startswith(f"{base}/")))


def _safe_norm_path(path: Any) -> str:
    raw = str(path or "").strip()
    if not raw.startswith("/") or any(part == ".." for part in raw.split("/")):
        return ""
    return posixpath.normpath(raw).rstrip("/")


def _require_evidence_equal(evidence: dict[str, Any], key: str, expected: str) -> None:
    actual = str(evidence.get(key) or "").rstrip("/")
    if actual != expected:
        raise _workload_mismatch(key, actual, expected)


def _require_evidence_int_equal(evidence: dict[str, Any], key: str, expected: int) -> None:
    actual = _optional_int(evidence.get(key))
    if actual != expected:
        raise _workload_mismatch(key, actual, str(expected))


def _workload_mismatch(key: str, actual: Any, expected: Any) -> APIError:
    return APIError(
        code="IDENTITY_WORKLOAD_MISMATCH",
        message=f"Claude workload identity evidence mismatch: {key}={actual!r}, expected {expected!r}",
        status_code=502,
        data={"field": key, "actual": actual, "expected": expected},
    )


def _shell_word(value: Any) -> str:
    import shlex

    return shlex.quote(str(value))


def _command_output(result: Any) -> str:
    logs = getattr(result, "logs", None)
    parts: list[str] = []
    if logs is not None:
        for stream in ("stdout", "stderr"):
            for item in getattr(logs, stream, []) or []:
                text = getattr(item, "text", None)
                parts.append(str(text if text is not None else item))
    for attr in ("stdout", "stderr", "output"):
        value = getattr(result, attr, None)
        if value:
            parts.append(str(value))
    error = getattr(result, "error", None)
    if error:
        parts.append(str(error))
    return "".join(parts)
