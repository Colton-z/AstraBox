"""DeepSeek Harness engine adapter (engine_kind="deepseek_harness").

The harness (`deepseek-ai/dsh`, MIT) is a TS coding agent that ships as a CLI
with several product shapes — a browser app, a terminal UI, a one-shot
headless run. AstraBox runs the browser one's server inside the sandbox and
speaks its API: the image installs the vendor's npm package and registers
``dsh --profile web`` as a supervised service, so the gateway is already
answering when the box comes up, and the adapter is a client of it.

That shape is deliberate. What the harness is — its tools, its prompt, its
modes, its approval policy — is composed by the vendor's own profile, exactly
as the Claude agent image composes itself. AstraBox supplies the box, the
credential, the workspace and the transport, and curates nothing.

On the conversation tenancy the runtime is a boot-time service of the box;
under the Agent-shared tenancy every conversation runs its own harness stack
inside its OpenSandbox isolated session (the image's
``serve-conversation.sh``, triggered through the launch declaration), and
the gateway port is derived from the conversation's uid. Either way a
reattach is a reconnect: the server outlives the host, so a restart
resolves the endpoint again and rejoins by naming the durable session key.
Activation returns :class:`DeepSeekHarnessRuntimeClient`, the conversation's
runtime contract (``engine/runtime.py``).

Self-registers as engine_kind="deepseek_harness" on import.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shlex
from typing import Any
from urllib.parse import urlparse

from astrabox.common.logger.logger_factory import get_logger
from astrabox.config.release_images import release_image
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EngineKind,
    EnginePreparationContext,
    EngineStartupContext,
    EngineStartupMaterialRequest,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    EngineSessionLogDeclaration,
    CONVERSATION_PLACEMENT_PER_ACCOUNT,
    EngineRuntimeCapabilities,
    EngineWorkloadDeclaration,
)
from astrabox.core.service.orchestrator.engine.extensions import (
    AGENTS_HOME_SKILLS_TARGET,
    EngineExtensions,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness_link import (
    DSH_API_PORT,
    DshApiLink,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness_runtime_client import (
    DSH_PERMISSION_PRESETS,
    ENGINE_KIND,
    DeepSeekHarnessLink,
    DeepSeekHarnessRuntimeClient,
    create_harness_session,
    resolve_model_route,
)
from astrabox.core.service.orchestrator.engine.provisioning import (
    EngineSandboxRequest,
    ModelCredentialRequest,
)
from astrabox.core.service.orchestrator.engine.registry import (
    register_engine_adapter,
)
from astrabox.core.service.orchestrator.engine.runtime import initialize_runtime_client
from astrabox.core.service.orchestrator.engine.runtime_profiles import (
    SANDBOX_IMAGE_WORKLOAD_HOME,
    SANDBOX_IMAGE_WORKLOAD_USER,
    SANDBOX_IMAGE_WORKSPACE_DIR,
)
from astrabox.core.service.orchestrator.runtime.config_resolver import (
    resolve_runtime_template_name,
)
from astrabox.core.service.orchestrator.runtime.models import SessionRuntime
from astrabox.core.service.orchestrator.runtime.sandbox_client import (
    extract_sandbox_id,
)
from astrabox.core.service.orchestrator.runtime.sandbox_script_writer import (
    install_verified_text_script,
)
from astrabox.seams.model import model_endpoint_for_name

logger = get_logger(__name__)

#: The AIO base image's own entrypoint. Required: the backend's ``None``
#: default is the Claude Code image's boot script, which this image does not
#: contain, so the container would exit 127 at boot.
DSH_IMAGE_ENTRYPOINT = ("/opt/gem/run.sh",)
DSH_RUNTIME_IMAGE_COMPONENT = "sandbox-deepseek-harness"

#: The environment variables the harness's DeepSeek provider plugin reads its
#: model access from. The harness chose these names. The generic gateway route
#: the client declares for other models names the same credential variable.
DSH_CREDENTIAL_ENV_VAR = "DEEPSEEK_API_KEY"
DSH_BASE_URL_ENV_VAR = "DEEPSEEK_BASE_URL"

#: An AstraBox variable, not one the harness defines. The image reads it at
#: boot to hand the working directory to the account its server runs as.
DSH_WORKSPACE_ENV_VAR = "ASTRABOX_WORKSPACE"

#: Native creation options are interpreted by the harness gateway.
DSH_ENGINE_OPTIONS_SCHEMA: tuple[dict[str, Any], ...] = (
    {
        "key": "session_create",
        "label": "DSH session creation",
        "type": "object",
        "protected_keys": ["cwd", "workspaceId", "sessionId"],
        "help": (
            "Overrides args.request of the native session/create Remote call. For example, "
            '{"agentPreset": "code"}. The harness interprets all native fields. '
            "Workspace and native session identity are platform-managed. "
            "Applied only when creating a conversation; reconnect keeps its configuration."
        ),
    },
    {
        "key": "profile_package",
        "label": "Profile package.json",
        "type": "object",
        "protected_keys": ["dsh"],
        "help": (
            "Fields of the conversation's web profile package.json, written before the "
            "harness starts and installed with `dsh plugin --profile web install`. Its "
            'dependencies are plugin Bundles, for example {"dependencies": {"humanizer-ru": '
            '"3.31.3"}}: npm versions, git specs or tarball URLs. The harness keeps the '
            "Bundle list (dsh.profile.bundles) in step with what is installed."
        ),
    },
)

#: Installs an Agent's plugin Bundles with the harness's own plugin command,
#: before the conversation's harness starts, which is when the harness reads
#: its Bundles. `dsh plugin --profile web` initializes the profile from the
#: shipped template when missing and forwards to pnpm in the profile
#: directory; a run that adds a dependency whose manifest declares a Bundle
#: also appends it to `dsh.profile.bundles`, and a run that removes one drops
#: it (apps/cli reference, "dsh plugin"). Writing `dependencies` and running
#: `install` adds nothing to that list, so the dependencies go through `add`
#: and `remove`, and the owner's other fields are written as they are.
_DSH_PROFILE_PACKAGE_STEP = r"""export PATH="/opt/dsh/node_modules/.bin:$PATH"
profile="$dsh_home/profiles/web"
dsh plugin --profile web ls >/dev/null
plan="$(python3 - "$profile/package.json" @FIELDS@ <<'PY'
import json, sys

path, fields = sys.argv[1], json.loads(sys.argv[2])
with open(path, encoding="utf-8") as handle:
    manifest = json.load(handle)
wanted = fields.pop("dependencies", None) or {}
current = manifest.get("dependencies") or {}
manifest.update(fields)
with open(path, "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, indent=2)
print(json.dumps({
    "remove": sorted(name for name in current if name not in wanted),
    "add": [f"{name}@{spec}" for name, spec in sorted(wanted.items()) if current.get(name) != spec],
}))
PY
)"
mapfile -t remove < <(python3 -c 'import json, sys; print("\n".join(json.loads(sys.argv[1])["remove"]))' "$plan" | sed '/^$/d')
mapfile -t add < <(python3 -c 'import json, sys; print("\n".join(json.loads(sys.argv[1])["add"]))' "$plan" | sed '/^$/d')
if [ "${#remove[@]}" -gt 0 ]; then
  dsh plugin --profile web remove "${remove[@]}"
fi
if [ "${#add[@]}" -gt 0 ]; then
  dsh plugin --profile web add "${add[@]}"
fi
"""

#: The author's MCP servers as rows of the harness's own MCP client
#: (`@deepseek-ai/dsh-mcp-client`: `serverName`, `transport` stdio or
#: streamable-http, `command`/`args`/`env` or `url`/`headers`), inserted by
#: the home patch layer, `$DSH_HOME/cordis.patch.yml`, which the harness
#: composes over its Bundles and profile when it starts and below the
#: platform's own overlay. Only the platform writes that file in a
#: conversation's home; JSON is YAML.
_DSH_HOME_PATCH_STEP = r"""printf '%s\n' @PATCH@ > "$dsh_home/cordis.patch.yml"
"""

_DSH_MCP_CLIENT = "@deepseek-ai/dsh-mcp-client"

#: Restarts the image's resident harness so it reads what the bootstrap just
#: installed: its Bundle set is fixed when it starts. The launcher wrapper
#: removes the launch URL file when it starts and writes it once the harness
#: prints its address (astrabox-dsh-web), so its reappearance is readiness.
_DSH_RESIDENT_RESTART_SCRIPT = r"""set -euo pipefail
launch="$HOME/.deepseek-harness/web-url"
rm -f "$launch"
supervisorctl restart astrabox-dsh >/dev/null
for _ in $(seq 1 600); do
  if [ -s "$launch" ]; then
    exit 0
  fi
  sleep 0.1
done
echo "the resident harness did not report its address within 60s of a restart" >&2
exit 1
"""

#: The registry pnpm resolves a version or range from when the profile names
#: no other; the harness's CLI forwards to pnpm unchanged.
_NPM_REGISTRY_HOST = "registry.npmjs.org"
_URL_HOST = re.compile(r"^(?:git\+)?[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([^/:?#]+)", re.IGNORECASE)
_SCP_HOST = re.compile(r"^(?:git\+)?[^@/\s]+@([^:/\s]+):")
_GITHUB_SHORTHAND = re.compile(r"^(?:github:)?[\w.-]+/[\w.-]+(?:#.*)?$")


def _dependency_hosts(spec: str) -> list[str]:
    """The hosts pnpm reaches for one dependency spec (pnpm's spec forms)."""

    value = spec.strip()
    if value.startswith(("file:", "link:", "workspace:")):
        return []
    match = _URL_HOST.match(value) or _SCP_HOST.match(value)
    if match:
        return [match.group(1).lower()]
    if _GITHUB_SHORTHAND.match(value):
        # pnpm fetches a GitHub dependency as a tarball from codeload.
        return ["github.com", "codeload.github.com"]
    return [_NPM_REGISTRY_HOST]


#: Where an AstraBox Agent's instructions reach this engine. The harness's
#: ``dsh-agent-instructions`` plugin reads project instructions from
#: ``AGENTS.md`` in the working directory — the vendor's own extension point,
#: the counterpart of Claude Code's ``CLAUDE.md``. Using it is the difference
#: between an Agent whose instructions take effect and a field that looks
#: configurable and is silently dropped.
DSH_AGENT_INSTRUCTIONS_FILE = "AGENTS.md"


#: Where the harness keeps its session logs, and the namespace their scopes
#: take in the platform's transcript store. Named here and in the image that
#: installs the relay, on opposite sides of one box.
DSH_SESSIONS_DIR_TEMPLATE = "{home}/.dsh/sessions"
DSH_MIRROR_SUBPATH_PREFIX = "dsh/"


def _dsh_mcp_row(name: str, server: dict[str, Any]) -> dict[str, Any]:
    """One author server as a `dsh-mcp-client` row (packages/mcp/mcp-client at dsh-v0.2.0-rc.2)."""

    config: dict[str, Any] = {"serverName": name}
    if server["type"] == "stdio":
        config.update(transport="stdio", command=server["command"], args=list(server.get("args") or []))
        if server.get("env"):
            config["env"] = dict(server["env"])
    elif server["type"] == "http":
        config.update(transport="streamable-http", url=server["url"])
        if server.get("headers"):
            config["headers"] = dict(server["headers"])
    else:
        raise ValueError(
            f"deepseek_harness takes stdio and streamable HTTP MCP servers, not {server['type']!r} ({name})"
        )
    return {"id": f"astrabox-mcp-{name}", "name": _DSH_MCP_CLIENT, "config": config}


def _conversation_gateway_port(runtime_identity: dict[str, Any] | None) -> int:
    """The port this conversation's harness answers on.

    The box tenancy publishes one gateway per box on the image's fixed
    forwarder port. Under the Agent-shared tenancy every conversation runs
    its own stack inside its isolated session, and the outward port is the
    platform's placement rule — derived from the conversation's uid, which
    the identity carries to every attach point.
    """

    identity = dict(runtime_identity or {})
    if str(identity.get("sandbox_tenancy") or "").strip() != "agent":
        return DSH_API_PORT
    uid = identity.get("uid")
    if uid is None:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                "an agent-tenancy deepseek_harness conversation carries no "
                "uid; its per-conversation gateway port cannot be derived"
            ),
            status_code=500,
        )
    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        runner_port_for_uid,
    )

    return runner_port_for_uid(int(uid))


def _native_model_base_url(model_access: Any, base_url: str) -> str:
    """Map the harness's native wire through the selected model provider."""
    if not base_url:
        return base_url
    provider = model_endpoint_for_name(str(getattr(model_access, "endpoint_provider", "") or ""))
    native = provider.protocol_base_url(base_url=base_url, protocol="deepseek-anthropic")
    common_url, native_url = urlparse(base_url), urlparse(native)
    common_path = common_url.path.rstrip("/")
    native_path = native_url.path.rstrip("/")
    if (
        (common_url.scheme, common_url.hostname, common_url.port)
        != (native_url.scheme, native_url.hostname, native_url.port)
        or native_url.query or native_url.fragment or native_url.username
        or not (native_path == common_path or native_path.startswith(common_path + "/"))
    ):
        raise APIError(
            code="MODEL_ENDPOINT_INVALID",
            message="the native model protocol must stay under its configured gateway base URL",
            status_code=500,
        )
    return native


def _engine_sandbox_request(
    model_access: Any, base_url: str
) -> EngineSandboxRequest:
    """What a harness box must be, for the cold create and the prepared box alike.

    One builder for both paths so a box prepared before its Session exists is
    the same box a cold start would have created. That is not tidiness: the
    server's trusted-host fence, its workspace ownership, its model route and
    its readiness meaning are all decided by this declaration at boot, and a
    prepared box that differed in any of them would fail at claim in a way
    that reads as a dead box rather than as a drifted declaration.
    """

    # Native and generic protocols have separate addresses on one gateway.
    # DSH appends /v1 to its native base; pi-ai keeps the generic base unchanged.
    native_base_url = _native_model_base_url(model_access, base_url)
    native_path = urlparse(native_base_url).path.rstrip("/")
    if not native_path.endswith("/v1"):
        native_path += "/v1"
    common_path = urlparse(base_url).path.rstrip("/")
    messages_prefix = native_path[len(common_path):].strip("/")
    messages_prefix = messages_prefix + "/" if messages_prefix else ""
    return EngineSandboxRequest(
        entrypoint=DSH_IMAGE_ENTRYPOINT,
        credential=ModelCredentialRequest(
            access=model_access,
            request_paths=(
                "chat/completions", f"{messages_prefix}messages",
                f"{messages_prefix}files", f"{messages_prefix}files/*",
            ),
            request_methods=("GET", "POST", "DELETE"),
            missing_code="ENGINE_CAPABILITY_UNAVAILABLE",
            missing_message=(
                "the deepseek_harness environment resolves no model "
                "gateway credential/base URL; configure the "
                "environment's model access"
            ),
        ),
        credential_env_var=DSH_CREDENTIAL_ENV_VAR,
        # The image hands this directory to its workload account
        # before starting the server; the platform is what decides
        # which directory, so it names the variable and fills it.
        cwd_env_var=DSH_WORKSPACE_ENV_VAR,
        env={DSH_BASE_URL_ENV_VAR: native_base_url},
        platform_mcp_servers_env=True,
        # The API server starts with the box, and the image opens this
        # port only once the server answers, so blocking the create on
        # it makes READY mean "can hold a conversation" rather than
        # "the container exists". The provider probes TCP acceptance
        # and nothing more — deliberately, since the field names a
        # port and not a protocol — so the meaning of accepting is the
        # image's to establish
        # (containers/sandbox-deepseek-harness/astrabox-dsh-forward).
        wait_for_inbox_service_port=DSH_API_PORT,
    )


def _slot_spawn_fingerprint(template: Any, *, base_url: str, native_base_url: str) -> str:
    """The box-frozen facts a prepared harness box answers for, as one digest.

    Covers what preparation fixes and no later RPC can change: the image
    (which carries the pinned vendor release, the profile overlay and the
    mirror's root/glob/namespace) with its entrypoint, the gateway base URL
    the resident server reads at boot and the egress allowlist is built from,
    the workload identity contract the workspace renders from — and the two
    facts the prepared CONVERSATION is composed with.

    The preset and the instructions are here because preparation now creates
    the conversation: the harness pins a preset into a session when it makes
    it, and the instructions plugin composes AGENTS.md at that same moment.
    A claim rejoins that conversation by name and can re-assert neither, so a
    box prepared for the old Agent must be retired rather than served. The
    permission preset stays out — it is a ``commands/execute`` line every
    publish re-asserts, start and rejoin alike.

    A claim recomputes this digest from the current Agent and discards a box
    whose value differs.
    """

    payload = {
        "engine_kind": ENGINE_KIND,
        "image": resolve_runtime_template_name(template),
        "entrypoint": list(DSH_IMAGE_ENTRYPOINT),
        "base_url": str(base_url or ""),
        "native_base_url": native_base_url,
        "workload_user": SANDBOX_IMAGE_WORKLOAD_USER,
        "workload_home": SANDBOX_IMAGE_WORKLOAD_HOME,
        "workspace": SANDBOX_IMAGE_WORKSPACE_DIR,
        "session_create": _session_create_options(template),
        "instructions": hashlib.sha256(
            str(getattr(template, "system", None) or "").strip().encode("utf-8")
        ).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


async def _install_agent_instructions(
    sandbox: Any, *, cwd: str, instructions: str,
    runtime_identity: dict[str, Any] | None,
) -> None:
    """Place the Agent's instructions where the harness already looks.

    Written into the conversation's own workspace, before the session is
    created, so the plugin reads them on its first composition. A pooled box
    is not a problem here: the workspace is per-conversation and the platform
    already creates it at claim time — this is content in it, not a capability
    assembled into the box.

    Written as the box's root, like everything else the command channel
    places, and left 0644 in a workspace the image has already handed to the
    workload account — so the harness reads it and an agent asked to revise
    its own instructions can replace it.
    """

    await install_verified_text_script(
        sandbox,
        path=f"{cwd.rstrip('/')}/{DSH_AGENT_INSTRUCTIONS_FILE}",
        content=instructions if instructions.endswith("\n") else instructions + "\n",
        runtime_identity=runtime_identity,
        mode=0o644,
        error_code="AGENT_RUNTIME_ERROR",
        error_message="failed to install the deepseek_harness agent instructions",
    )


def _session_create_options(template: Any) -> dict[str, Any]:
    """Read one native creation payload without a supplier-field whitelist."""
    from astrabox.core.service.orchestrator.schema_validation import validate_declared_config_bag

    bag = getattr(template, "engine_options", None)
    if bag is None:
        return {}
    validate_declared_config_bag(
        bag, DSH_ENGINE_OPTIONS_SCHEMA,
        bag_label="engine_options", owner_label="engine 'deepseek_harness'",
    )
    return dict(bag.get("session_create", {}))


async def _publish_runtime(
    *,
    session_id: str,
    sandbox: Any,
    link: DeepSeekHarnessLink,
    engine_session_key: str | None,
    resume_session_key: str | None,
    terminal_cwd: str,
    runtime_identity: dict[str, Any] | None = None,
    permission_mode: str | None = None,
    model: str | None = None,
    gateway_base_url: str = "",
    model_endpoint_provider: str = "",
    prepared_model_route: dict[str, Any] | None = None,
    session_create: dict[str, Any] | None = None,
    prepare_engine_input: Any = None,
) -> SessionRuntime:
    """One live link → one conversation-bound, published SessionRuntime.

    ``engine_session_key`` names the harness conversation to rejoin (a resume
    or a prepared box's); without one the conversation is created here with
    the Agent's creation options. ``resume_session_key`` is what the platform
    asked to resume, which the client must answer under.
    """

    native = engine_session_key or await create_harness_session(
        link, cwd=terminal_cwd, session_create=session_create
    )
    engine_client = DeepSeekHarnessRuntimeClient(
        link=link,
        native_session_id=native,
        model_endpoint_provider=model_endpoint_for_name(model_endpoint_provider),
    )
    engine_manifest = await initialize_runtime_client(
        engine_client, engine_kind=ENGINE_KIND, resume_session_key=resume_session_key,
    )
    if permission_mode:
        # Applied on every publish, start and reattach alike. The harness pins
        # a preset into a session when it creates it and selecting the one it
        # already holds appends nothing, so re-asserting is cheap and it is
        # what makes the claim below true rather than assumed.
        await engine_client.set_permission_mode(permission_mode)
    if model:
        # The engine session carries no model of its own — `session/create`
        # has no such field — so this is the only place the Environment's
        # choice reaches the harness. Asserted on every publish for the same
        # reason the preset is: a model changed on the Agent has to reach the
        # conversations that already exist.
        await engine_client.select_model(
            model,
            gateway_base_url=gateway_base_url,
            gateway_key_env=DSH_CREDENTIAL_ENV_VAR,
            prepared_route=(
                str(prepared_model_route.get("route") or "") or None
                if prepared_model_route and prepared_model_route.get("model") == model
                else None
            ),
        )
    runtime = SessionRuntime(
        session_id=session_id,
        agent=None,
        engine_kind=ENGINE_KIND,
        engine_client=engine_client,
        engine_manifest=engine_manifest,
        sandbox=sandbox,
        sandbox_id=extract_sandbox_id(sandbox),
        # Under the Agent-shared tenancy the box is shared; the isolated
        # session is what teardown closes and what a reattach rebuilds from.
        isolated_session_id=(
            str((runtime_identity or {}).get("isolated_session_id") or "")
            or None
        ),
        engine_session_key=engine_client.engine_session_key,
        terminal_cwd=terminal_cwd,
        permission_mode=permission_mode,
        runtime_identity=runtime_identity,
        prepare_engine_input=prepare_engine_input,
    )
    if permission_mode:
        # AstraBox reconciles the mode before every dispatch and skips that
        # round trip only when the runtime reports it verified. Omitting this
        # flag fails the next dispatch with "permission_mode reconciliation
        # was not accepted before dispatch", because the reconciliation runs
        # before the runtime is published and finds no engine client.
        # ``hermes.py`` sets the same flag.
        setattr(runtime, "permission_mode_verified", True)
    return runtime


class DeepSeekHarnessEngineAdapter(EngineAdapter):
    """EngineAdapter for the DeepSeek Harness web profile's API."""

    def child_run_is_active(self, child_run: dict[str, Any]) -> bool:
        from astrabox.core.service.orchestrator.engine.child_runs import (
            ChildRunProjectionError,
        )
        if child_run.get("closed") is True:
            return False
        activity = child_run.get("engine_status")
        if activity not in {"running", "inactive"}:
            raise ChildRunProjectionError(
                f"DeepSeek Harness child has unknown activity {activity!r}"
            )
        return activity == "running"

    @property
    def engine_kind(self) -> EngineKind:
        return ENGINE_KIND

    @property
    def engine_client_type(self) -> type[Any]:
        return DeepSeekHarnessRuntimeClient

    @property
    def capabilities(self) -> EngineRuntimeCapabilities:
        return EngineRuntimeCapabilities(
            engine_kind=ENGINE_KIND,
            supported_session_kinds=frozenset({"agent_chat"}),
            workload=EngineWorkloadDeclaration(
                # The harness keeps its own state under $DSH_HOME, not in
                # a per-conversation dot-directory the platform renders.
                config_dir_name=None,
                required_commands=(
                    "bash",
                    "getent",
                    "runuser",
                    "id",
                    "mkdir",
                    "chown",
                    "chmod",
                    "/usr/local/bin/astrabox-provision-conversation",
                ),
            ),
            conversation_placement=CONVERSATION_PLACEMENT_PER_ACCOUNT,
            default_runtime_image=release_image(DSH_RUNTIME_IMAGE_COMPONENT),
            # The harness's filesystem Skill provider scans
            # $DSH_AGENTS_HOME/skills, which defaults to ~/.agents/skills.
            extensions=EngineExtensions(
                mcp_transports=frozenset({"stdio", "http"}),
                skills_target=AGENTS_HOME_SKILLS_TARGET,
                native_config=DSH_ENGINE_OPTIONS_SCHEMA,
            ),
            # Bootstrap minimum for the pinned web profile. The live client
            # reads the complete permission preset list from the process
            # catalog, so an additive vendor preset reaches the console
            # without changing this declaration. Removing one of these is a
            # breaking profile change because the default depends on it.
            permission_modes=DSH_PERMISSION_PRESETS,
            permission_mode_defaults=(("agent_chat", "workspace-write"),),
            # The harness writes one append-only log per conversation and, like
            # Codex, hands none of it over its protocol — the session id it
            # minted is a handle to a file, and a box without that file answers
            # `session-not-found`. The image's profile overlay selects the
            # plaintext encoding this relay reads; the compressed default would
            # leave no boundary to stop a batch at short of the next turn.
            session_log=EngineSessionLogDeclaration(
                root_template=DSH_SESSIONS_DIR_TEMPLATE,
                namespace=DSH_MIRROR_SUBPATH_PREFIX,
            ),
        )

    # This engine can prepare either placement the platform selects. Under
    # conversation tenancy the whole box carries the image-resident harness
    # server. Under Agent tenancy the platform starts that same image-owned
    # service inside the slot's isolated account, and preparation creates the
    # conversation it will later rejoin. This flag declares that the whole-box
    # form is supported as well; it does not exclude the shared-slot form.
    prepares_conversation_box = True

    async def prepare_runtime(
        self, context: EnginePreparationContext
    ) -> dict[str, Any]:
        """Prepare the harness inside a platform-owned placement.

        Connect to the web-profile server for this placement: the image's
        boot-time service for a conversation box, or the isolated account's
        service for a shared slot. Create the native session with the Agent's
        cwd and creation options so a later claim can rejoin it by name.
        ``session/create`` took 9.4s of a measured 26s cold path.

        A session log with nowhere to send its bytes reads to the in-box relay
        as a misconfiguration, and it exits FATAL past its grace. The unclaimed
        marker makes undelivered preparation legitimate until a stated
        deadline; claim overwrites it with the real target before first input.

        The connection reaches the vendor's HTTP server through the endpoint
        a claim will use, beyond the forwarder's TCP port check. Closing the
        link releases its event streams while retaining the native session.

        Allocation, storage, credentials, mirror readiness and cleanup remain
        platform work. This method only establishes the vendor conversation
        that a later Session will adopt.
        """

        target_slot = str(context.slot_id or "").strip()
        if not target_slot:
            raise APIError(
                code="AGENT_PREWARM_CONFIG_INVALID",
                message="prepared deepseek_harness box requires a slot id",
                status_code=500,
            )
        template = context.template
        identity = context.runtime_identity
        base_url = str(getattr(context.model_access, "base_url", "") or "").strip()
        instructions = str(getattr(template, "system", None) or "").strip()
        if instructions:
            await _install_agent_instructions(
                context.sandbox, cwd=context.cwd, instructions=instructions,
                runtime_identity=identity,
            )
        link = await DshApiLink.connect(
            context.sandbox,
            port=_conversation_gateway_port(identity),
            launch_url_path=f"{(identity or {}).get('home_dir') or SANDBOX_IMAGE_WORKLOAD_HOME}/.deepseek-harness/web-url",
        )
        model = str(getattr(context.model_access, "model_name", "") or "").strip()
        try:
            prepared_native_session = await create_harness_session(
                link,
                cwd=context.cwd,
                session_create=_session_create_options(template),
            )
            # Resolving the route reads the harness's model catalog and writes
            # its gateway settings, about 190 ms; done here, a claim only
            # selects the model on the conversation it rejoins.
            prepared_model_route = (
                {
                    "model": model,
                    "route": await resolve_model_route(
                        link,
                        model,
                        gateway_base_url=base_url,
                        gateway_key_env=DSH_CREDENTIAL_ENV_VAR,
                        model_endpoint_provider=model_endpoint_for_name(
                            str(getattr(context.model_access, "endpoint_provider", "") or "")
                        ),
                    ),
                }
                if model
                else None
            )
        finally:
            await link.close()
        logger.info(
            "prepared deepseek_harness box: slot=%s box=%s cwd=%s",
            target_slot,
            context.sandbox_id,
            context.cwd,
        )
        return {
            "engine_kind": ENGINE_KIND,
            "spawn_fingerprint": _slot_spawn_fingerprint(
                template, base_url=base_url,
                native_base_url=_native_model_base_url(context.model_access, base_url),
            ),
            # The claim rejoins this conversation. Measured `session/create`
            # takes 9.4s of the 26s cold path.
            "prepared_native_session": prepared_native_session,
            # The harness route resolved for the Agent's model in this box.
            "prepared_model_route": prepared_model_route,
            # Platform MCP clients discovered tools at boot using the slot
            # binding. Claim binds that address before rejoining this session.
            # Agent-declared MCP servers remain unsupported.
            "activation_mcp_servers": [],
        }

    def sandbox_request(
        self,
        *,
        template: Any,
        model_access: Any,
    ) -> EngineSandboxRequest:
        """Declare the harness image and wire without allocating anything."""

        base_url = str(getattr(model_access, "base_url", "") or "").strip()
        return _engine_sandbox_request(model_access, base_url)

    def startup_material_request(
        self, *, template: Any, model_access: Any, deployment_settings: Any,
    ) -> EngineStartupMaterialRequest:
        """Each native profile belongs to one conversation's isolated account."""
        return EngineStartupMaterialRequest(
            runtime_state_store=True, runtime_state_scope="session",
        )

    async def activate_runtime(
        self,
        context: EngineStartupContext,
    ) -> SessionRuntime:
        """Open the harness protocol in a platform-prepared runtime."""

        if context.attach_mode is not None and not str(
            context.resume_session_key or ""
        ).strip():
            raise APIError(
                code="ENGINE_RUNTIME_UNAVAILABLE",
                message=(
                    f"deepseek_harness session {context.session_id!r} carries no "
                    "engine session key to rejoin. Start a new conversation."
                ),
                status_code=409,
            )
        template = context.template
        prepared = context.prepared_manifest or {}
        is_prepared = context.prepared_manifest is not None
        base_url = str(getattr(context.model_access, "base_url", "") or "").strip()
        if str(prepared.get("placement") or "") == "conversation_box":
            expected_spawn = _slot_spawn_fingerprint(
                template, base_url=base_url,
                native_base_url=_native_model_base_url(context.model_access, base_url),
            )
            if expected_spawn != str(prepared.get("spawn_fingerprint") or ""):
                raise APIError(
                    code="AGENT_PREWARM_CONFIG_INVALID",
                    message=(
                        "the prepared harness box was built from a different "
                        "image, gateway, preset, or instruction set"
                    ),
                    status_code=409,
                )
        prepared_native_session = str(
            prepared.get("prepared_native_session") or ""
        ).strip()
        if is_prepared and not prepared_native_session:
            raise APIError(
                code="AGENT_PREWARM_CONFIG_INVALID",
                message=(
                    "claimed prepared runtime names no prepared conversation; "
                    "a claim rejoins one and cannot create it"
                ),
                status_code=409,
            )
        sandbox = context.sandbox
        link: DeepSeekHarnessLink | None = None
        try:
            if not context.runtime_state_store:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="platform startup supplied no DSH native-state target",
                    status_code=500,
                )
            home = str((context.runtime_identity or {}).get("home_dir") or "")
            if not home.startswith("/"):
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="DSH native state requires the placed runtime home",
                    status_code=500,
                )
            state_target_path = f"{home.rstrip('/')}/.dsh/astrabox-runtime-state.json"
            state_target = json.dumps(
                context.runtime_state_store, ensure_ascii=False, sort_keys=True
            )
            linux_user = str(context.runtime_identity["linux_user"])
            if is_prepared:
                # A claimed runtime has never held a Session's state target,
                # and the image reads it only when the attachment restore
                # below asks, so one owner-only write publishes it whole
                # without the installer's atomic replacement.
                await sandbox.files.write_bytes(
                    state_target_path,
                    state_target.encode("utf-8"),
                    mode=0o600,
                    owner=linux_user,
                    group=linux_user,
                )
            else:
                await install_verified_text_script(
                    sandbox,
                    path=state_target_path,
                    content=state_target,
                    runtime_identity=context.runtime_identity,
                    mode=0o600,
                    owner_user=linux_user,
                    error_code="AGENT_RUNTIME_ERROR",
                    error_message="failed to install the DSH native-state target",
                )
            instructions = str(getattr(template, "system", None) or "").strip()
            if instructions and not is_prepared:
                await _install_agent_instructions(
                    sandbox, cwd=context.cwd, instructions=instructions,
                    runtime_identity=context.runtime_identity,
                )
            link = await DshApiLink.connect(
                sandbox,
                port=_conversation_gateway_port(context.runtime_identity),
                launch_url_path=f"{(context.runtime_identity or {}).get('home_dir') or SANDBOX_IMAGE_WORKLOAD_HOME}/.deepseek-harness/web-url",
            )
            await link.restore_attachments()
            return await _publish_runtime(
                session_id=context.session_id,
                sandbox=sandbox,
                link=link,
                engine_session_key=(
                    context.resume_session_key or prepared_native_session or None
                ),
                resume_session_key=context.resume_session_key,
                terminal_cwd=context.cwd,
                runtime_identity=context.runtime_identity,
                permission_mode=context.permission_mode,
                model=str(getattr(context.model_access, "model_name", "") or "").strip() or None,
                gateway_base_url=base_url,
                model_endpoint_provider=str(context.model_access.endpoint_provider),
                prepared_model_route=prepared.get("prepared_model_route"),
                session_create=_session_create_options(template),
                prepare_engine_input=context.prepare_engine_input,
            )
        except BaseException:
            if link is not None:
                with contextlib.suppress(BaseException):
                    await link.close()
            raise

    def install_egress_hosts(self, engine_options: dict[str, Any]) -> list[str]:
        """The hosts pnpm reaches for the profile package's dependencies."""

        fields = engine_options.get("profile_package")
        dependencies = fields.get("dependencies") if isinstance(fields, dict) else None
        hosts: list[str] = []
        for spec in (dependencies or {}).values() if isinstance(dependencies, dict) else ():
            for host in _dependency_hosts(str(spec)):
                if host not in hosts:
                    hosts.append(host)
        return hosts

    def conversation_install_script(
        self,
        *,
        packages: list[dict[str, str]],
        engine_options: dict[str, Any],
        mcp_servers: dict[str, dict[str, Any]],
    ) -> str | None:
        """Write the author's MCP rows and install the Agent's profile package fields."""

        _ = packages
        steps = ['set -euo pipefail', 'dsh_home="${DSH_HOME:-$HOME/.dsh}"', 'mkdir -p "$dsh_home"']
        if mcp_servers:
            rows = [_dsh_mcp_row(name, server) for name, server in mcp_servers.items()]
            steps.append(
                _DSH_HOME_PATCH_STEP.replace("@PATCH@", shlex.quote(json.dumps([{"insert": rows}])))
            )
        fields = engine_options.get("profile_package")
        if isinstance(fields, dict) and fields:
            steps.append(_DSH_PROFILE_PACKAGE_STEP.replace("@FIELDS@", shlex.quote(json.dumps(fields))))
        if len(steps) == 3:
            return None
        return "\n".join(steps) + "\nprintf 'DSH_EXTENSIONS_READY\\n'\n"

    def resident_restart_script(self) -> str | None:
        """Restart the image's harness: it reads its Bundles only when it starts."""

        return _DSH_RESIDENT_RESTART_SCRIPT

    def shared_conversation_service_launch(
        self, *, home: str, workspace: str, port: int
    ) -> str:
        """One conversation's harness stack, as one backgrounded trigger.

        The image's ``serve-conversation.sh`` owns the mechanism (harness on
        loopback, this conversation's own mirror, an answer-gated forwarder
        on the outward port — see that script for why the order IS the
        readiness contract); the trigger carries only the outward port, which
        is the platform's placement rule.
        """

        import shlex

        # The isolated session's shell does not set the account's HOME, and
        # the script derives every per-conversation path from it, so the
        # trigger must carry it. TMPDIR points at a private-home spool for the
        # same reason claude_code's trigger does: the session's /tmp is not
        # in the isolation policy's writable roots, and the engine writes
        # os.tmpdir() at boot.
        log = f"{home}/.astrabox-dsh-launch.log"
        spool = f"{home}/.astrabox-spool"
        return (
            f"mkdir -p {shlex.quote(spool)} && "
            f"HOME={shlex.quote(home)} "
            f"TMPDIR={shlex.quote(spool)} "
            f"setsid /opt/dsh/serve-conversation.sh {int(port)} "
            f"</dev/null >>{shlex.quote(log)} 2>&1 &"
        )

register_engine_adapter(ENGINE_KIND, DeepSeekHarnessEngineAdapter())
