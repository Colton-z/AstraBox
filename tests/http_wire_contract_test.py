"""Snapshot the complete HTTP surface produced by ``create_app()``.

The contract contains the recursive FastAPI route table and a canonical OpenAPI
document. The schema is stored as reviewable JSON and protected by a SHA-256
digest.

The fixture fixes environment-dependent inputs so local state cannot change the
snapshot: identity providers, feature flags, allowed hosts, tracing, env-file
loading, and the frontend distribution path. An empty frontend directory keeps
the API-only route shape deterministic.

The test constructs the app without entering its lifespan, so it does not touch
the network, database, or sandbox runtime.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

_DATA_DIR = Path(__file__).parent / "data"
_OPENAPI_SNAPSHOT_PATH = _DATA_DIR / "openapi_snapshot.json"

#: sha256 of ``_canonical_openapi_json(app.openapi())`` for the current tree —
#: i.e. ``sha256(_OPENAPI_SNAPSHOT_PATH.read_bytes())``. The two are asserted
#: independently (see ``test_openapi_schema_matches_frozen_hash`` /
#: ``test_openapi_schema_matches_committed_snapshot_file``) so tampering with
#: just the file, or just this constant, still fails loud.
_OPENAPI_SHA256 = "469f190abb0bec1c3d4db9044d3427826d0a7a55611a76c70897ab1e4fe65344"

#: The frozen ROUTE TABLE of the current tree. One row per route OBJECT (not
#: per HTTP method) — ``methods`` carries every verb registered on that one
#: route object, matching how FastAPI/Starlette actually store a decorated
#: handler (e.g. the built-in ``/docs`` carries ``("GET", "HEAD")`` on a single
#: ``Route``. Most application routes use a single-verb decorator; the raw
#: provider callback intentionally preserves the platform's HTTP method and is
#: therefore one multi-verb route object. Extension proxy routes are also
#: multi-verb by design.
#:
#: Sorted by ``(path, methods, name)`` for human scanability; the comparison
#: in the test sorts both sides the same way, so this literal's on-disk order
#: is cosmetic, not load-bearing.
_EXPECTED_ROUTES: list[tuple[tuple[str, ...], str, str]] = [
    (("GET", "HEAD"), "/_next/{asset_path:path}", "extension_console_asset"),
    (
        ("POST",),
        "/api/v1/admin-api/assistants/{assistant_id}/workspace/hibernate",
        "admin_api_hibernate_assistant_workspace",
    ),
    (
        ("POST",),
        "/api/v1/admin-api/assistants/{assistant_id}/workspace/wake",
        "admin_api_wake_assistant_workspace",
    ),
    (("GET",), "/api/v1/admin-api/errors", "admin_api_list_errors"),
    (("GET",), "/api/v1/admin-api/process/health", "admin_api_process_health"),
    (("GET",), "/api/v1/admin-api/sessions/all", "admin_api_list_sessions"),
    (("GET",), "/api/v1/admin/agent-schema", "get_agent_schema_endpoint"),
    (("GET",), "/api/v1/admin/agents/{agent_id}/credential-vaults", "get_agent_credential_binding"),
    (("PUT",), "/api/v1/admin/agents/{agent_id}/credential-vaults", "set_agent_credential_binding"),
    (("GET",), "/api/v1/admin/agents/{agent_id}/deployments", "list_agent_deployments"),
    (("POST",), "/api/v1/admin/agents/{agent_id}/deployments", "create_agent_deployment"),
    (
        ("DELETE",),
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}",
        "delete_agent_deployment",
    ),
    (
        ("PUT",),
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}",
        "update_agent_deployment",
    ),
    (
        ("GET",),
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/runs",
        "list_deployment_runs",
    ),
    (
        ("POST",),
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/runs",
        "trigger_deployment_run",
    ),
    (
        ("POST",),
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/runs/{run_id}/replay",
        "replay_deployment_run",
    ),
    (("GET",), "/api/v1/admin/agents/{agent_id}/mcp-servers", "get_agent_mcp_servers"),
    (("PUT",), "/api/v1/admin/agents/{agent_id}/mcp-servers", "set_agent_mcp_servers"),
    (
        ("GET",),
        "/api/v1/admin/assistants/{assistant_id}/credential-vaults",
        "get_assistant_credential_binding",
    ),
    (
        ("PUT",),
        "/api/v1/admin/assistants/{assistant_id}/credential-vaults",
        "set_assistant_credential_binding",
    ),
    (("GET",), "/api/v1/admin/channel-providers", "list_channel_providers"),
    (("GET",), "/api/v1/admin/deployments", "list_deployments"),
    (("GET",), "/api/v1/admin/environment-schema", "get_environment_schema_endpoint"),
    (("GET",), "/api/v1/admin/environments", "list_admin_environments"),
    (("PUT",), "/api/v1/admin/environments/{name}", "upsert_admin_environment"),
    (("GET",), "/api/v1/admin/environments/{name}/models", "list_environment_models"),
    (("GET",), "/api/v1/admin/errors", "admin_list_errors"),
    (("GET",), "/api/v1/admin/integrations", "admin_integrations"),
    (("GET",), "/api/v1/admin/logs", "admin_logs"),
    (("GET",), "/api/v1/admin/mcp-servers", "list_mcp_servers"),
    (("POST",), "/api/v1/admin/mcp-servers", "create_mcp_server"),
    (("DELETE",), "/api/v1/admin/mcp-servers/{mcp_server_id}", "delete_mcp_server"),
    (("GET",), "/api/v1/admin/mcp-servers/{mcp_server_id}", "get_mcp_server"),
    (("PATCH",), "/api/v1/admin/mcp-servers/{mcp_server_id}", "update_mcp_server"),
    (("GET",), "/api/v1/admin/navigation-summary", "admin_navigation_summary"),
    (("GET",), "/api/v1/admin/process/health", "admin_process_health"),
    (("GET",), "/api/v1/admin/sandbox-idle-action", "describe_sandbox_idle_action"),
    (("POST",), "/api/v1/admin/sandbox-idle-sweep", "run_sandbox_idle_sweep"),
    (("GET",), "/api/v1/admin/sandboxes", "list_sandboxes"),
    (("GET",), "/api/v1/admin/sandboxes/{sandbox_id}", "describe_sandbox"),
    (
        ("GET",),
        "/api/v1/admin/sandboxes/{sandbox_id}/diagnostics/{scope}",
        "read_sandbox_diagnostics",
    ),
    (("GET",), "/api/v1/admin/sandboxes/{sandbox_id}/security", "read_sandbox_security"),
    (("GET",), "/api/v1/admin/sessions/all", "admin_list_all_sessions"),
    (("GET",), "/api/v1/admin/sessions/transcripts", "admin_batch_session_transcripts"),
    (("GET",), "/api/v1/admin/sessions/{session_id}/detail", "admin_session_detail"),
    (("POST",), "/api/v1/admin/sessions/{session_id}/evict-runtime", "admin_evict_runtime"),
    (("POST",), "/api/v1/admin/sessions/{session_id}/kill", "admin_kill_session"),
    (("GET",), "/api/v1/admin/sessions/{session_id}/trace", "admin_session_trace"),
    (("GET",), "/api/v1/admin/sessions/{session_id}/transcript", "admin_session_transcript"),
    (("GET",), "/api/v1/admin/system/overview", "admin_system_overview"),
    (("GET",), "/api/v1/admin/vaults", "list_vaults"),
    (("POST",), "/api/v1/admin/vaults", "create_vault"),
    (("DELETE",), "/api/v1/admin/vaults/{vault_id}", "delete_vault"),
    (("GET",), "/api/v1/admin/vaults/{vault_id}", "get_vault"),
    (("POST",), "/api/v1/admin/vaults/{vault_id}/archive", "archive_vault"),
    (("GET",), "/api/v1/admin/vaults/{vault_id}/bindings", "list_vault_bindings"),
    (("GET",), "/api/v1/admin/vaults/{vault_id}/credentials", "list_credentials"),
    (("POST",), "/api/v1/admin/vaults/{vault_id}/credentials", "create_credential"),
    (
        ("DELETE",),
        "/api/v1/admin/vaults/{vault_id}/credentials/{credential_id}",
        "delete_credential",
    ),
    (
        ("PATCH",),
        "/api/v1/admin/vaults/{vault_id}/credentials/{credential_id}",
        "update_credential",
    ),
    (
        ("POST",),
        "/api/v1/admin/vaults/{vault_id}/credentials/{credential_id}/archive",
        "archive_credential",
    ),
    (("GET",), "/api/v1/agent-configuration/environments", "list_agent_configuration_environments"),
    (
        ("GET",),
        "/api/v1/agent-configuration/environments/{name}/models",
        "list_agent_configuration_models",
    ),
    (("GET",), "/api/v1/agent-configuration/schema", "get_agent_configuration_schema"),
    (("GET",), "/api/v1/agents", "list_agents"),
    (("POST",), "/api/v1/agents", "create_agent"),
    (("DELETE",), "/api/v1/agents/{agent_id}", "delete_agent"),
    (("GET",), "/api/v1/agents/{agent_id}", "get_agent"),
    (("PUT",), "/api/v1/agents/{agent_id}", "update_agent"),
    (("GET",), "/api/v1/agents/{agent_id}/access", "get_agent_access"),
    (("PUT",), "/api/v1/agents/{agent_id}/access", "set_agent_access"),
    (("POST",), "/api/v1/agents/{agent_id}/conversations", "start_agent_conversation"),
    (
        ("POST",),
        "/api/v1/agents/{agent_id}/extension-console/session",
        "create_extension_console_session",
    ),
    (("GET",), "/api/v1/agents/{agent_id}/extensions", "get_agent_extensions"),
    (("PUT",), "/api/v1/agents/{agent_id}/extensions", "set_agent_extensions"),
    (("POST",), "/api/v1/agents/{agent_id}/hibernate", "hibernate_agent"),
    (
        ("GET",),
        "/api/v1/agents/{agent_id}/prepared-runtime",
        "get_agent_prepared_runtime",
    ),
    (
        ("POST",),
        "/api/v1/agents/{agent_id}/prepared-runtime/refresh",
        "refresh_agent_prepared_runtime",
    ),
    (("GET",), "/api/v1/agents/{agent_id}/sessions", "list_agent_sessions"),
    (("POST",), "/api/v1/agents/{agent_id}/wake", "wake_agent"),
    (("GET",), "/api/v1/assistants", "list_assistants"),
    (("POST",), "/api/v1/assistants", "create_assistant"),
    (("DELETE",), "/api/v1/assistants/{assistant_id}", "delete_assistant"),
    (("GET",), "/api/v1/assistants/{assistant_id}", "get_assistant"),
    (("PATCH",), "/api/v1/assistants/{assistant_id}", "update_assistant"),
    (("POST",), "/api/v1/assistants/{assistant_id}/conversations", "start_assistant_conversation"),
    (("DELETE",), "/api/v1/assistants/{assistant_id}/workspace", "destroy_workspace"),
    (("POST",), "/api/v1/assistants/{assistant_id}/workspace/hibernate", "hibernate_workspace"),
    (("POST",), "/api/v1/assistants/{assistant_id}/workspace/wake", "wake_workspace"),
    (
        ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"),
        "/api/v1/deployments/{deployment_id}/callback/{callback_path:path}",
        "channel_callback",
    ),
    (("POST",), "/api/v1/deployments/{deployment_id}/trigger", "trigger_deployment"),
    (("GET",), "/api/v1/exposed-ports/{deployment_id}/{port}/url", "refresh_exposed_port_url"),
    (("GET",), "/api/v1/mcp", "agent_mcp_event_stream"),
    (("POST",), "/api/v1/mcp", "agent_mcp_endpoint"),
    (("POST",), "/api/v1/mcp-tokens", "issue_mcp_token"),
    (("GET",), "/api/v1/mcp-tokens", "list_mcp_tokens"),
    (("DELETE",), "/api/v1/mcp-tokens/{token_id}", "revoke_mcp_token"),
    (
        ("POST",),
        "/api/v1/platform-mcp/{deployment_id}/{server_name}/mcp",
        "platform_mcp_streamable_http",
    ),
    (
        ("POST",),
        "/api/v1/sandbox-callback/{subject_type}/{subject_id}/{generation}/{token}",
        "sandbox_lifecycle_callback",
    ),
    (
        ("POST",),
        "/api/v1/sbxcap/{cap_token}/api/v1/runtime-state/load",
        "runtime_state_load",
    ),
    (
        ("POST",),
        "/api/v1/sbxcap/{cap_token}/api/v1/runtime-state/save",
        "runtime_state_save",
    ),
    (
        ("POST",),
        "/api/v1/sbxcap/{cap_token}/api/v1/sandbox/{sandbox_id}/terminating",
        "sandbox_terminating_notice",
    ),
    (
        ("POST",),
        "/api/v1/sbxcap/{cap_token}/api/v1/transcript/{session_id}/append",
        "transcript_append_scoped",
    ),
    (
        ("POST",),
        "/api/v1/sbxcap/{cap_token}/api/v1/transcript/{session_id}/list-sessions",
        "transcript_list_sessions_scoped",
    ),
    (
        ("POST",),
        "/api/v1/sbxcap/{cap_token}/api/v1/transcript/{session_id}/list-subkeys",
        "transcript_list_subkeys_scoped",
    ),
    (
        ("POST",),
        "/api/v1/sbxcap/{cap_token}/api/v1/transcript/{session_id}/load",
        "transcript_load_scoped",
    ),
    (("GET",), "/api/v1/sessions", "list_sessions"),
    (("DELETE",), "/api/v1/sessions/{session_id}", "delete_session"),
    (("GET",), "/api/v1/sessions/{session_id}", "get_session"),
    (("GET",), "/api/v1/sessions/{session_id}/ai-stream", "resume_ai_stream"),
    (("POST",), "/api/v1/sessions/{session_id}/ai-stream", "send_message_ai_stream"),
    (("POST",), "/api/v1/sessions/{session_id}/archive", "archive_session"),
    (("GET",), "/api/v1/sessions/{session_id}/child-runs", "list_session_child_runs"),
    (
        ("GET",),
        "/api/v1/sessions/{session_id}/child-runs/{child_run_id}/messages",
        "get_session_child_run_messages",
    ),
    (
        ("POST",),
        "/api/v1/sessions/{session_id}/child-runs/{child_run_id}/stop",
        "stop_session_child_run",
    ),
    (("POST",), "/api/v1/sessions/{session_id}/conversation/end", "end_conversation"),
    (("POST",), "/api/v1/sessions/{session_id}/files/delete", "delete_session_files"),
    (("GET",), "/api/v1/sessions/{session_id}/files/download", "download_session_file"),
    (("POST",), "/api/v1/sessions/{session_id}/files/list", "list_session_files"),
    (("POST",), "/api/v1/sessions/{session_id}/files/mkdir", "mkdir_session_files"),
    (("POST",), "/api/v1/sessions/{session_id}/files/move", "move_session_files"),
    (("POST",), "/api/v1/sessions/{session_id}/files/upload", "upload_session_files"),
    (
        ("GET",),
        "/api/v1/sessions/{session_id}/history-blocks",
        "get_session_history_blocks",
    ),
    (
        ("GET",),
        "/api/v1/sessions/{session_id}/history-blocks/{block_id}",
        "get_session_history_block_details",
    ),
    (("POST",), "/api/v1/sessions/{session_id}/interaction-respond", "interaction_respond"),
    (("POST",), "/api/v1/sessions/{session_id}/interrupt", "interrupt_session"),
    (("GET",), "/api/v1/sessions/{session_id}/messages", "get_messages"),
    (
        ("POST",),
        "/api/v1/sessions/{session_id}/messages/{message_id}/process-summary",
        "generate_session_process_summary",
    ),
    (("POST",), "/api/v1/sessions/{session_id}/permission-mode", "update_session_permission_mode"),
    (("POST",), "/api/v1/sessions/{session_id}/recover", "recover_session"),
    (("POST",), "/api/v1/sessions/{session_id}/sandbox/terminate", "terminate_sandbox"),
    (("DELETE",), "/api/v1/sessions/{session_id}/share", "revoke_share"),
    (("GET",), "/api/v1/sessions/{session_id}/share", "get_share"),
    (("POST",), "/api/v1/sessions/{session_id}/share", "create_share"),
    (("POST",), "/api/v1/sessions/{session_id}/terminal/stream", "terminal_stream"),
    (("POST",), "/api/v1/sessions/{session_id}/turn-inputs", "append_turn_input"),
    (("GET",), "/api/v1/sessions/{session_id}/webshell", "get_webshell"),
    (("GET",), "/api/v1/share/{token}", "shared_session"),
    (("GET",), "/api/v1/share/{token}/files/download", "shared_file_download"),
    (("GET",), "/api/v1/share/{token}/files/list", "shared_files_list"),
    (("GET",), "/api/v1/share/{token}/history-blocks", "shared_history_blocks"),
    (("GET",), "/api/v1/share/{token}/history-blocks/{block_id}", "shared_history_block_details"),
    (("GET",), "/api/v1/share/{token}/messages", "shared_messages"),
    (("POST",), "/api/v1/transcript/{session_id}/append", "transcript_append"),
    (("POST",), "/api/v1/transcript/{session_id}/list-sessions", "transcript_list_sessions"),
    (("POST",), "/api/v1/transcript/{session_id}/list-subkeys", "transcript_list_subkeys"),
    (("POST",), "/api/v1/transcript/{session_id}/load", "transcript_load"),
    (("GET",), "/api/v1/user/current", "get_current_user"),
    (
        ("DELETE", "GET", "PATCH", "POST", "PUT"),
        "/claude-code/{extension_path:path}",
        "extension_console_claude_code",
    ),
    (("GET", "HEAD"), "/docs", "swagger_ui_html"),
    (("GET", "HEAD"), "/docs/oauth2-redirect", "swagger_ui_redirect"),
    (("GET",), "/extension-console/open", "open_extension_console"),
    (("GET",), "/get/mcp_semantic_filter_settings", "extension_console_mcp_semantic_filter"),
    (("GET",), "/healthz", "healthz"),
    (
        ("DELETE", "GET", "HEAD", "PATCH", "POST", "PUT"),
        "/key/{blocked_path:path}",
        "extension_console_gateway_refused",
    ),
    (("DELETE", "GET", "HEAD", "PATCH", "POST", "PUT"), "/litellm", "litellm_admin_root"),
    (
        ("GET", "HEAD"),
        "/litellm-asset-prefix/_next/{asset_path:path}",
        "extension_console_prefixed_asset",
    ),
    (
        ("DELETE", "GET", "HEAD", "PATCH", "POST", "PUT"),
        "/litellm/{upstream_path:path}",
        "litellm_admin_proxy",
    ),
    (("GET", "POST"), "/mcp-rest/{extension_path:path}", "extension_console_mcp_runtime"),
    (("GET",), "/metrics", "metrics"),
    (("GET",), "/model_group/info", "extension_console_model_group_info"),
    (("GET", "HEAD"), "/openapi.json", "openapi"),
    (("GET",), "/public/skill_hub", "extension_console_skill_hub"),
    (("GET",), "/readyz", "readyz"),
    (("GET", "HEAD"), "/redoc", "redoc_html"),
    (("GET", "HEAD"), "/ui/{ui_path:path}", "extension_console_ui"),
    (
        ("DELETE", "GET", "PATCH", "POST", "PUT"),
        "/v1/mcp/{extension_path:path}",
        "extension_console_mcp",
    ),
    (
        ("DELETE", "GET", "HEAD", "PATCH", "POST", "PUT"),
        "/v1/{blocked_path:path}",
        "extension_console_gateway_refused",
    ),
]

#: Documentation-level sanity constants, redundant with the full-list equality
#: check but give a clearer top-line failure signal than a 100+ row list diff.
_EXPECTED_ROUTE_COUNT = 168
_EXPECTED_DISTINCT_PATH_COUNT = 141


def _effective_routes(app: Any) -> list[tuple[tuple[str, ...], str, str]]:
    """Flatten every route ``create_app()`` registers to ``(methods, path, name)``.

    FastAPI 0.139 stores an ``include_router(...)`` target as a lazy
    ``_IncludedRouter`` holder on the parent's ``.routes`` rather than eagerly
    copying its children up — walking only ``app.routes`` would therefore miss
    every route mounted through ``app.include_router(_astrabox.router)``.
    Recursing into ``.original_router.routes`` (mirroring
    ``tests/app_boot_test.py``'s ``_iter_route_paths``) resolves this; the
    child paths are already fully-qualified (``/api/v1/...``) because the
    router is constructed with no ``prefix=`` of its own and ``include_router``
    here is called with no ``prefix=`` either — verified by
    inspection, not assumed.
    """
    rows: list[tuple[tuple[str, ...], str, str]] = []

    def walk(routes: Any) -> None:
        for route in routes:
            inner = getattr(route, "original_router", None) or getattr(route, "router", None)
            if inner is not None and getattr(inner, "routes", None) is not None:
                walk(inner.routes)
                continue
            path = getattr(route, "path", None)
            if not isinstance(path, str):
                continue
            methods = tuple(sorted(getattr(route, "methods", None) or ()))
            name = str(getattr(route, "name", None) or "")
            rows.append((methods, path, name))

    walk(app.routes)
    return rows


def _canonical_openapi_json(schema: dict[str, Any]) -> str:
    """Canonical (sorted-key, 2-space, real-unicode) serialization of a schema.

    Used identically to produce ``tests/data/openapi_snapshot.json`` and to
    re-serialize the live schema at test time, so the two are compared
    byte-for-byte rather than by some looser structural equality.
    """
    return json.dumps(schema, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


@contextmanager
def _deterministic_construction_env(frontend_dist: Path) -> Iterator[None]:
    """Monkeypatch exactly the env ``create_app()`` construction is sensitive
    to (see the module docstring), scoped to the ``with`` block so it never
    leaks into another test.
    """
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("ASTRABOX_ENV_FILE", str(frontend_dist.parent / "unused.env"))
        mp.setenv("ASTRABOX_WEB_IDENTITY", "local")
        mp.setenv("ASTRABOX_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver")
        mp.setenv("ASTRABOX_AGENT_ENABLED", "true")
        mp.setenv("ASTRABOX_FRONTEND_DIST", str(frontend_dist))
        # Build on the tracing-OFF path: init_tracing(app) reads this during
        # construction (instrument_app is middleware-only and cannot move the
        # route/openapi pin, but keep the build hermetic on any machine).
        mp.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        yield
    finally:
        mp.undo()


def _build_app(frontend_dist: Path) -> Any:
    """Construct the real app deterministically (no network; the app OBJECT
    only — the lifespan never runs)."""
    with _deterministic_construction_env(frontend_dist):
        from astrabox.api.app import create_app

        return create_app()


@pytest.fixture(scope="module")
def wire_app(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The app built once for this module's read-only route/schema pins."""
    frontend_dist = tmp_path_factory.mktemp("wire-contract") / "no-frontend-dist"
    return _build_app(frontend_dist)


# ── route table pin ──────────────────────────────────────────────────────────


def test_route_table_matches_frozen_snapshot(wire_app: Any) -> None:
    actual = sorted(_effective_routes(wire_app), key=lambda r: (r[1], r[0], r[2]))
    expected = sorted(_EXPECTED_ROUTES, key=lambda r: (r[1], r[0], r[2]))
    assert actual == expected


def test_route_table_counts_match_documented_totals(wire_app: Any) -> None:
    rows = _effective_routes(wire_app)
    assert len(rows) == _EXPECTED_ROUTE_COUNT
    assert len({path for _, path, _ in rows}) == _EXPECTED_DISTINCT_PATH_COUNT


def test_route_table_has_no_method_path_collisions(wire_app: Any) -> None:
    """No two DIFFERENT route objects claim the same (method, path).

    Starlette resolves routes in registration order and the first match wins,
    so a genuine collision would silently shadow one handler with another —
    this is a standing regression guard, independent of the frozen literal
    above, so a newly-introduced collision fails with a direct message rather
    than only showing up as an opaque list-diff.
    """
    seen: dict[tuple[str, str], str] = {}
    collisions: list[str] = []
    for methods, path, name in _effective_routes(wire_app):
        for method in methods:
            key = (method, path)
            if key in seen and seen[key] != name:
                collisions.append(f"{method} {path}: {seen[key]!r} vs {name!r}")
            else:
                seen[key] = name
    assert collisions == []


def test_route_registration_is_deterministic_across_constructions(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Two independent ``create_app()`` builds yield the identical route table.

    A standing guard for exactly the risk flagged while authoring this pin:
    each ``register_*_routes(app)`` module keys its idempotency guard off
    ``id(app)`` (a memory address), which CPython can reuse once the previous
    app object is garbage-collected — if that ever happens, a SECOND
    construction could silently register fewer routes than the first. This
    builds twice (fresh temp dirs, no shared state) and compares them to each
    other, so any nondeterminism — this one or a future one the E-b refactor
    might introduce — fails immediately instead of only showing up as
    unexplained flake in the frozen-snapshot test above.
    """
    dist_a = tmp_path_factory.mktemp("wire-contract-a") / "no-frontend-dist"
    dist_b = tmp_path_factory.mktemp("wire-contract-b") / "no-frontend-dist"
    first = sorted(_effective_routes(_build_app(dist_a)))
    second = sorted(_effective_routes(_build_app(dist_b)))
    assert first == second


# ── OpenAPI schema pin ───────────────────────────────────────────────────────


def test_openapi_schema_matches_frozen_hash(wire_app: Any) -> None:
    canonical = _canonical_openapi_json(wire_app.openapi())
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == _OPENAPI_SHA256


def test_openapi_schema_matches_committed_snapshot_file(wire_app: Any) -> None:
    canonical = _canonical_openapi_json(wire_app.openapi())
    on_disk = _OPENAPI_SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert canonical == on_disk


def test_openapi_schema_excludes_the_frontend_catch_all(wire_app: Any) -> None:
    # include_in_schema=False on the SPA catch-all (app.py::_mount_frontend)
    # means it never enters the OpenAPI pin regardless of whether it is
    # mounted; this is asserted directly rather than only implied by the hash.
    schema = wire_app.openapi()
    assert not any("full_path" in p for p in schema["paths"])
