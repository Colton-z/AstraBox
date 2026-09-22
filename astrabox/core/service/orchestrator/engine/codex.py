"""The `codex` engine: OpenAI's Codex CLI, driven through its app-server.

What AstraBox supplies is a box, a credential, a workspace and a connection.
What Codex supplies is the agent: its tools, its prompt, its sandbox
enforcement and its approval flow. The seam between them is `codex
app-server`, the JSON-RPC interface every OpenAI-built Codex surface runs on,
so nothing in this adapter re-implements a behaviour the CLI already has.

Two decisions here are worth reading before changing them.

**The model endpoint rides the protocol, not the image.** Codex takes a whole
`model_providers` table as a per-thread config override, so the gateway's base
URL travels with `thread/start` and the image carries no configuration file
and no per-deployment value. Only the API key is an environment variable,
because that is what a provider's `env_key` names.

**Its wire is the Responses API.** `wire_api` accepts exactly one value,
`"responses"`, so the credential proxy is told to admit that path and not
`chat/completions` — a gateway that serves only the completions path answers a
Codex turn with a 404 that looks like a model problem.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from typing import Any


from astrabox.common.logger.logger_factory import get_logger
from astrabox.config.release_images import release_image
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.emissions import (
    ChildResourceFact,
    emission_from_translated_frame,
)
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EngineClient,
    EngineConversationBinding,
    EngineKind,
    EnginePreparationContext,
    EngineStartupContext,
    initialize_engine_client,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    EngineSessionLogDeclaration,
    CONVERSATION_PLACEMENT_PER_ACCOUNT,
    EngineRuntimeCapabilities,
    EngineWorkloadDeclaration,
)
from astrabox.core.service.orchestrator.engine.codex_client import (
    CODEX_SANDBOX_MODES,
    ENGINE_KIND,
    CodexEngineClient,
)
from astrabox.core.service.orchestrator.engine.codex_link import (
    CODEX_APP_SERVER_PORT,
    CodexAppServerLink,
)
from astrabox.core.service.orchestrator.engine.provisioning import (
    EngineSandboxRequest,
    ModelCredentialRequest,
)
from astrabox.core.service.orchestrator.engine.registry import register_engine_adapter
from astrabox.core.service.orchestrator.runtime.config_resolver import (
    resolve_runtime_template_name,
)
from astrabox.core.service.orchestrator.engine.runtime_profiles import (
    SANDBOX_IMAGE_WORKLOAD_HOME,
    SANDBOX_IMAGE_WORKLOAD_USER,
    SANDBOX_IMAGE_WORKSPACE_DIR,
)
from astrabox.core.service.orchestrator.runtime.models import SessionRuntime
from astrabox.core.service.orchestrator.runtime.sandbox_client import extract_sandbox_id

logger = get_logger(__name__)

#: The AIO base image's own entrypoint. Required: the backend's ``None``
#: default is the Claude Code image's boot script, which this image does not
#: contain, so the container would exit 127 at boot.
CODEX_IMAGE_ENTRYPOINT = ("/opt/gem/run.sh",)

#: Release image component an Environment that pins nothing resolves to for
#: this engine (:func:`~astrabox.config.release_images.release_image`). Declared
#: because an adapter without one fails the resolve with a registered error
#: rather than inheriting another engine's image.
CODEX_RUNTIME_IMAGE_COMPONENT = "sandbox-codex"

#: The environment variable the provider config below names as its `env_key`.
#: Codex reads the key from the process environment, so this is the one value
#: that cannot ride the protocol with the rest of the provider table.
CODEX_CREDENTIAL_ENV_VAR = "OPENAI_API_KEY"

#: An AstraBox variable, not one Codex defines. The image reads it at boot to
#: hand the working directory to the account the app-server runs as.
CODEX_WORKSPACE_ENV_VAR = "ASTRABOX_WORKSPACE"

#: The provider id AstraBox registers inside a thread's config. Codex requires
#: `model_provider` to name an entry in `model_providers`, and the name is
#: only ever seen inside that pair.
CODEX_PROVIDER_ID = "astrabox"

#: Where Codex keeps rollouts, and the namespace the mirror files them under.
#: Named on both sides of the box: here, and as `SUBPATH_PREFIX` in
#: `astrabox-codex-mirror`.
CODEX_SESSIONS_DIR_TEMPLATE = "{home}/.codex/sessions"
CODEX_MIRROR_SUBPATH_PREFIX = "codex/"

#: The environment variable the image reads the catalog from.
CODEX_MODEL_CATALOG_ENV_VAR = "CODEX_MODEL_CATALOG_JSON"

CODEX_CONFIG_PROTECTED_KEYS = (
    "model",
    "model_provider",
    "model_providers",
    "model_catalog_json",
    "cwd",
    "sandbox_mode",
    "developer_instructions",
    "mcp_servers",
    "sandbox_workspace_write",
    "permissions",
)
CODEX_TURN_PROTECTED_KEYS = (
    "threadId",
    "input",
    "clientUserMessageId",
    "cwd",
    "model",
    "sandboxPolicy",
    "permissions",
    "runtimeWorkspaceRoots",
    "environments",
    "toolOutput",
)

CODEX_ENGINE_OPTIONS_SCHEMA: tuple[dict[str, Any], ...] = (
    {
        "key": "model_catalog",
        "label": "Model catalog",
        "type": "object",
        "help": (
            "The vendor's `models.json` for the models this environment "
            "serves. Replaces the complete models.json object at box startup. "
            "Codex looks its model up by slug here; without "
            "an entry it falls back to its behaviour for an unknown model, "
            "which against a gateway answers one message twice. The fields "
            "that decide the transport live in this file, so it is the "
            "engine's own answer rather than a display detail."
        ),
    },
    {
        "key": "config",
        "label": "Codex config overrides",
        "type": "object",
        "protected_keys": list(CODEX_CONFIG_PROTECTED_KEYS),
        "help": (
            "Passed as thread/start and thread/resume params.config: native "
            "Codex config.toml overrides, merged by Codex. For example "
            '{"approval_policy":"never","model_reasoning_effort":"high"}. '
            "Platform model routing, workspace, permissions, MCP and session "
            "storage nodes (including dotted descendants) cannot be overridden."
        ),
    },
    {
        "key": "turn_start",
        "label": "Codex turn/start options",
        "type": "object",
        "protected_keys": list(CODEX_TURN_PROTECTED_KEYS),
        "help": (
            "Native fields added to each turn/start params object, such as "
            "effort, outputSchema or collaborationMode. Values are passed "
            "unchanged; Codex validates them. Platform input, identity, workspace, "
            "model and permissions cannot be overridden. In collaborationMode, "
            "omit settings.model: AstraBox supplies the thread's effective model."
        ),
    },
)


def _engine_option(template: Any, key: str) -> dict[str, Any]:
    bag = getattr(template, "engine_options", None)
    if not isinstance(bag, dict):
        return {}
    value = bag.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"codex {key} must be a JSON object")
    protected = (
        CODEX_CONFIG_PROTECTED_KEYS
        if key == "config"
        else CODEX_TURN_PROTECTED_KEYS
        if key == "turn_start"
        else ()
    )
    for field in value:
        if field.split(".", 1)[0] in protected:
            raise ValueError(f"codex {key}.{field} is managed by AstraBox")
    collaboration = value.get("collaborationMode") if key == "turn_start" else None
    settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
    if isinstance(settings, dict) and "model" in settings:
        raise ValueError("codex turn_start.collaborationMode.settings.model is managed by AstraBox")
    return dict(value)


def _model_catalog(template: Any) -> str | None:
    if "model_catalog" not in (getattr(template, "engine_options", None) or {}):
        return None
    catalog = _engine_option(template, "model_catalog")
    return json.dumps(catalog, sort_keys=True)


def provider_config(base_url: str) -> dict[str, Any]:
    """The `model_providers` table one thread runs against.

    Built here rather than written into the image because the gateway's
    address is a deployment fact and the image is not per-deployment. Codex
    validates the pair itself: an unknown `model_provider` fails
    `thread/start` by name.
    """

    return {
        "model_provider": CODEX_PROVIDER_ID,
        # This provider terminates at the deployment's OpenAI-compatible
        # gateway, not OpenAI's hosted Responses service. Codex otherwise
        # enables hosted web search by default and publishes a `web_search`
        # tool that a function-only gateway cannot execute.
        "web_search": "disabled",
        "model_providers": {
            CODEX_PROVIDER_ID: {
                "name": "AstraBox model gateway",
                "base_url": base_url,
                "env_key": CODEX_CREDENTIAL_ENV_VAR,
                # The only value Codex accepts. Named rather than omitted so
                # the wire this adapter assumes is written down next to the
                # request path the credential proxy is told to admit.
                "wire_api": "responses",
            }
        },
        # The catalog is deliberately absent: the image's one catalog writer
        # names it in the serving account's own config.toml, and an override
        # here would name one account's path for every tenancy — the shared
        # tenancy's server then dies loading another account's home.
    }


def _conversation_app_server_port(
    runtime_identity: dict[str, Any] | None,
) -> int:
    """The port this conversation's app-server answers on.

    The box tenancy publishes one server per box on the image's fixed
    forwarder port. Under the Agent-shared tenancy every conversation runs
    its own stack inside its isolated session, and the outward port is the
    platform's placement rule — derived from the conversation's uid, which
    the identity carries to every attach point.
    """

    identity = dict(runtime_identity or {})
    if str(identity.get("sandbox_tenancy") or "").strip() != "agent":
        return CODEX_APP_SERVER_PORT
    uid = identity.get("uid")
    if uid is None:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                "an agent-tenancy codex conversation carries no uid; its "
                "per-conversation app-server port cannot be derived"
            ),
            status_code=500,
        )
    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        runner_port_for_uid,
    )

    return runner_port_for_uid(int(uid))


def _engine_sandbox_request(model_access: Any, catalog: str | None) -> EngineSandboxRequest:
    """What a codex box must be, for the cold create and the prepared box alike.

    One builder for both paths so a box prepared before its Session exists is
    the same box a cold start would have created — the entrypoint, the
    catalog, the credential wire and the readiness port cannot drift between
    them.
    """

    return EngineSandboxRequest(
        entrypoint=CODEX_IMAGE_ENTRYPOINT,
        # The image writes this to `models.json` before starting the server;
        # it is a box-create fact because the server reads its catalog once,
        # at boot.
        env=({CODEX_MODEL_CATALOG_ENV_VAR: catalog} if catalog else {}),
        credential=ModelCredentialRequest(
            access=model_access,
            # Codex's only wire is the Responses API, so this is the path the
            # proxy may attach the real credential to.
            request_paths=("responses",),
            missing_code="ENGINE_CAPABILITY_UNAVAILABLE",
            missing_message=(
                "the codex environment resolves no model gateway "
                "credential/base URL; configure the environment's "
                "model access"
            ),
        ),
        credential_env_var=CODEX_CREDENTIAL_ENV_VAR,
        # The image hands this directory to its workload account before
        # starting the server; the platform decides which directory, so it
        # names the variable and fills it.
        cwd_env_var=CODEX_WORKSPACE_ENV_VAR,
        # The app-server starts with the box, and the image opens this port
        # only once the server answers, so blocking the create on it makes
        # READY mean "can hold a conversation".
        wait_for_inbox_service_port=CODEX_APP_SERVER_PORT,
    )


def _slot_spawn_fingerprint(template: Any, *, base_url: str, catalog: str | None) -> str:
    """The box-frozen facts a prepared codex box answers for, as one digest.

    Covers exactly what the box CREATE fixes and `thread/start` cannot
    override: the image and its entrypoint, the model catalog baked into
    `models.json` at boot, the gateway base URL frozen into the egress
    allowlist, and the workload identity contract the workspace/cwd renders
    from. Thread-borne facts — model, instructions, sandbox mode, approval
    policy, collaboration mode — are deliberately absent: they ride the
    activation RPC onto the live runtime, so changing them must NOT retire a
    prepared box. A claim recomputes this from the current Agent and discards
    a box whose digest differs (the stale catalog answers one message twice;
    the stale allowlist dead-ends the model route).
    """

    payload = {
        "engine_kind": ENGINE_KIND,
        "image": resolve_runtime_template_name(template),
        "entrypoint": list(CODEX_IMAGE_ENTRYPOINT),
        "model_catalog_json": str(catalog or ""),
        "base_url": str(base_url or ""),
        "workload_user": SANDBOX_IMAGE_WORKLOAD_USER,
        "workload_home": SANDBOX_IMAGE_WORKLOAD_HOME,
        "workspace": SANDBOX_IMAGE_WORKSPACE_DIR,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


async def _publish_runtime(
    *,
    session_id: str,
    sandbox: Any,
    link: CodexAppServerLink,
    engine_session_key: str | None,
    terminal_cwd: str,
    runtime_identity: dict[str, Any] | None = None,
    permission_mode: str | None = None,
    instructions: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    config: dict[str, Any] | None = None,
    turn_start: dict[str, Any] | None = None,
    model_catalog: bool = False,
    prepare_engine_input: Any = None,
    resident_output_sink: Any = None,
    event_sink: Any = None,
) -> SessionRuntime:
    """One live link → one conversation-bound, published SessionRuntime.

    Shared by the start and reattach flows so binding and publication cannot
    drift between them. The thread is created — or rejoined by name — inside
    `initialize_engine_client`, which is where the binding contract holds.
    """

    engine_client = CodexEngineClient(
        session_id=session_id,
        link=link,
        native_thread_id=engine_session_key,
        cwd=terminal_cwd,
        instructions=instructions,
        model=model,
        turn_options=turn_start,
        thread_config={**(provider_config(base_url) if base_url else {}), **(config or {})},
        # Start only: a rejoin names a thread that already has its mode, and
        # `thread/start`'s `sandbox` field has no meaning on a resume.
        sandbox_mode=permission_mode if engine_session_key is None else None,
        resident_output_sink=resident_output_sink,
        event_sink=event_sink,
    )
    engine_manifest = await initialize_engine_client(
        engine_client,
        expected_engine_kind=ENGINE_KIND,
        conversation_binding=EngineConversationBinding(
            platform_session_id=session_id,
            engine_session_key=engine_session_key,
        ),
    )
    if permission_mode:
        # Re-asserted on every publish, start and reattach alike. On a start
        # the thread already carries it and this only records the choice; on a
        # reattach it is what arms the next turn's override, which is the only
        # way a resumed thread's enforcement can be moved.
        await engine_client.set_permission_mode(permission_mode)
    runtime = SessionRuntime(
        session_id=session_id,
        agent=None,
        engine_kind=ENGINE_KIND,
        engine_client=engine_client,
        engine_manifest=engine_manifest,
        conversation_bound=True,
        sandbox=sandbox,
        sandbox_id=extract_sandbox_id(sandbox),
        # Under the Agent-shared tenancy the box is shared; the isolated
        # session is what teardown closes, what a reattach rebuilds from, and
        # half of the consumption receipts' carrier identity.
        isolated_session_id=(
            str((runtime_identity or {}).get("isolated_session_id") or "") or None
        ),
        engine_session_key=engine_client.engine_session_key,
        terminal_cwd=terminal_cwd,
        permission_mode=permission_mode,
        runtime_identity=runtime_identity,
        prepare_engine_input=prepare_engine_input,
    )
    if permission_mode:
        # The platform reconciles the mode before every dispatch and skips
        # that round trip only when the runtime reports it verified; without
        # the flag the next dispatch fails with "permission_mode
        # reconciliation was not accepted before dispatch".
        setattr(runtime, "permission_mode_verified", True)
    return runtime


class CodexEngineAdapter(EngineAdapter):
    """EngineAdapter for the Codex CLI's app-server."""

    def child_run_is_active(self, child_run: dict[str, Any]) -> bool:
        from astrabox.core.service.orchestrator.engine.child_runs import (
            ChildRunProjectionError,
        )
        from astrabox.core.service.orchestrator.engine.codex_child_runs import (
            _LIVE_TURN_STATUS,
            _TERMINAL_TURN_STATUSES,
        )

        if child_run.get("closed") is True:
            return False
        status = child_run.get("engine_status")
        if status == _LIVE_TURN_STATUS:
            return True
        # A newly discovered thread need not have started a turn yet.
        if not status or status in _TERMINAL_TURN_STATUSES:
            return False
        raise ChildRunProjectionError(f"Codex child turn has unknown status {status!r}")

    def durable_child_resource_facts(
        self,
        raw_messages: list[dict[str, Any]],
    ) -> list[tuple[int, ChildResourceFact]]:
        """Child facts from the native reads and turn/item notifications journaled.

        While no run was open, the relay persisted each child thread it read
        as ``{"method": "thread/read", "thread": ...}``. The document names its
        own parent through ``source.subAgent.thread_spawn.parent_thread_id``,
        so a projection per parent thread replays the same fold the live
        reader used. Native turn/item notifications fill the live-history gap using
        that same projection, without manufacturing a thread/read snapshot.
        """

        from astrabox.core.service.orchestrator.engine.codex_child_runs import (
            THREAD_READ_METHOD,
            CodexChildResources,
        )

        async def _no_call(method: str, params: dict[str, Any]) -> Any:
            raise RuntimeError("durable fold reads nothing from the engine")

        projectors: dict[str, CodexChildResources] = {}
        facts: list[tuple[int, ChildResourceFact]] = []
        for index, record in enumerate(raw_messages):
            if not isinstance(record, dict):
                continue
            if record.get("method") in {"turn/started", "turn/completed", "item/started", "item/completed"}:
                for known_projector in projectors.values():
                    for frame in known_projector.fold_notification(record):
                        emission = emission_from_translated_frame(frame)
                        if isinstance(emission, ChildResourceFact):
                            facts.append((index, emission))
                continue
            if record.get("method") != THREAD_READ_METHOD:
                continue
            thread = record.get("thread")
            if not isinstance(thread, dict):
                continue
            source = thread.get("source")
            sub_agent = source.get("subAgent") if isinstance(source, dict) else None
            spawn = sub_agent.get("thread_spawn") if isinstance(sub_agent, dict) else None
            parent = str((spawn or {}).get("parent_thread_id") or "").strip()
            if not parent:
                continue
            projector = projectors.get(parent)
            if projector is None:
                projector = projectors[parent] = CodexChildResources(
                    root_thread_id=parent, call=_no_call
                )
            for frame in projector.fold_thread(thread):
                emission = emission_from_translated_frame(frame)
                if isinstance(emission, ChildResourceFact):
                    facts.append((index, emission))
        return facts

    @property
    def engine_kind(self) -> EngineKind:
        return ENGINE_KIND

    @property
    def engine_client_type(self) -> type[EngineClient]:
        return CodexEngineClient

    @property
    def capabilities(self) -> EngineRuntimeCapabilities:
        return EngineRuntimeCapabilities(
            engine_kind=ENGINE_KIND,
            supported_session_kinds=frozenset({"agent_chat"}),
            workload=EngineWorkloadDeclaration(
                # Codex keeps its threads under $CODEX_HOME, not in a
                # per-conversation dot-directory the platform renders.
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
            # `SandboxMode` verbatim. Read off a running app-server rather
            # than transcribed: `permissionProfile/list` answers with the same
            # three, spelled as profile ids (`:workspace`), and those are
            # config-selected while these are what `thread/start` takes.
            default_runtime_image=release_image(CODEX_RUNTIME_IMAGE_COMPONENT),
            permission_modes=CODEX_SANDBOX_MODES,
            permission_mode_defaults=(("agent_chat", "workspace-write"),),
            # Codex writes the rollout and mentions it to nobody: no protocol
            # frame carries a line of it, and `thread/resume` rebuilds a thread
            # from that file and nothing else. Declaring it is what makes the
            # platform move the bytes out of the box and back into its
            # replacement — the adapter does none of it.
            session_log=EngineSessionLogDeclaration(
                root_template=CODEX_SESSIONS_DIR_TEMPLATE,
                namespace=CODEX_MIRROR_SUBPATH_PREFIX,
            ),
            engine_options_schema=CODEX_ENGINE_OPTIONS_SCHEMA,
        )

    def shared_conversation_service_launch(self, *, home: str, workspace: str, port: int) -> str:
        """One conversation's Codex stack, as one backgrounded trigger.

        The image's ``serve-conversation.sh`` owns the mechanism (app-server
        on a per-conversation unix socket in this home, this conversation's
        own mirror, and the upgrade-proving forwarder on the outward port —
        see that script and forward.py for why the order IS the readiness
        contract); the trigger carries only the outward port, the platform's
        placement rule. The engine environment (model catalog, diagnostics)
        arrives through the home's ``.astrabox-engine-env``, which the script
        requires.
        """

        import shlex

        # The isolated session's shell does not set the account's HOME, and
        # the script derives every per-conversation path from it, so the
        # trigger must carry it. TMPDIR points at a private-home spool for the
        # same reason claude_code's trigger does: the session's /tmp is not
        # in the isolation policy's writable roots, and the engine writes
        # os.tmpdir() at boot.
        log = f"{home}/.astrabox-codex-launch.log"
        spool = f"{home}/.astrabox-spool"
        return (
            f"mkdir -p {shlex.quote(spool)} && "
            f"HOME={shlex.quote(home)} "
            f"TMPDIR={shlex.quote(spool)} "
            f"setsid /opt/codex/serve-conversation.sh {int(port)} "
            f"</dev/null >>{shlex.quote(log)} 2>&1 &"
        )

    # The prepared unit is one whole box carrying the resident app-server —
    # there is no per-session engine child to park, so a per-conversation box
    # with no runner is exactly this engine's preparation form.
    prepares_conversation_box = True

    async def prepare_runtime(self, context: EnginePreparationContext) -> dict[str, Any]:
        """Prove Codex in the placement the platform already prepared.

        Codex's input-free preparation seam is `codex app-server` itself: the
        image runs it session-free and resident (one server per box holds
        every conversation in it — `containers/sandbox-codex/
        astrabox-codex-serve`), so preparing is creating the box and proving
        the server answers. Nothing engine-side is per-session: `thread/start`
        carries cwd, model, the provider table, instructions, sandbox mode and
        approval policy at claim, and the vendor requires the `initialize`
        handshake per CONNECTION, so the probe socket opened here leaves the
        server exactly as a boot with no probe would.

        Allocation, credential delivery and cleanup are already complete. A
        shared placement's image-owned service was proven by the platform; a
        whole box additionally receives a real app-server handshake here.
        """

        if context.placement == "shared_slot":
            return {}

        target_slot = str(context.slot_id or "").strip()
        if not target_slot:
            raise APIError(
                code="AGENT_PREWARM_CONFIG_INVALID",
                message="prepared codex box requires a slot id",
                status_code=500,
            )
        template = context.template
        base_url = str(getattr(context.model_access, "base_url", "") or "").strip()
        catalog = _model_catalog(template)
        link = None
        try:
            link = await CodexAppServerLink.connect(context.sandbox)
            server_info = link.server_info or {}
            await link.close()
        except BaseException:
            if link is not None:
                with contextlib.suppress(BaseException):
                    await link.close()
            raise
        logger.info(
            "prepared codex box: slot=%s box=%s server=%s",
            target_slot,
            context.sandbox_id,
            str(server_info.get("userAgent") or "") or "initialized",
        )
        return {
            "engine_kind": ENGINE_KIND,
            "spawn_fingerprint": _slot_spawn_fingerprint(
                template, base_url=base_url, catalog=catalog
            ),
            # Only user-identity MCP servers belong in the activation set;
            # codex declares no MCP servers, so the set is empty.
            "activation_mcp_servers": [],
        }

    def sandbox_request(
        self,
        *,
        template: Any,
        model_access: Any,
    ) -> EngineSandboxRequest:
        """Declare Codex's image contract; the platform owns realization."""

        catalog = _model_catalog(template)
        return _engine_sandbox_request(model_access, catalog)

    async def activate_runtime(
        self,
        context: EngineStartupContext,
    ) -> SessionRuntime:
        """Connect Codex to a box whose lifecycle is already settled."""

        if context.attach_mode is not None and not str(context.resume_session_key or "").strip():
            raise APIError(
                code="ENGINE_RUNTIME_UNAVAILABLE",
                message=(
                    f"codex session {context.session_id!r} carries no thread id "
                    "to rejoin. Start a new conversation."
                ),
                status_code=409,
            )
        template = context.template
        model_access = context.model_access
        base_url = str(getattr(model_access, "base_url", "") or "").strip()
        if not base_url:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="codex model gateway base url is missing",
                status_code=500,
            )
        catalog = _model_catalog(template)
        prepared = context.prepared_manifest or {}
        if str(prepared.get("placement") or "") == "conversation_box":
            expected_spawn = _slot_spawn_fingerprint(template, base_url=base_url, catalog=catalog)
            if expected_spawn != str(prepared.get("spawn_fingerprint") or ""):
                raise APIError(
                    code="AGENT_PREWARM_CONFIG_INVALID",
                    message=(
                        "the prepared Codex box was built from a different "
                        "image, gateway, or model catalog"
                    ),
                    status_code=409,
                )

        sandbox = context.sandbox
        link: CodexAppServerLink | None = None
        try:
            link = await CodexAppServerLink.connect(
                sandbox,
                port=_conversation_app_server_port(context.runtime_identity),
            )
            return await _publish_runtime(
                session_id=context.session_id,
                resident_output_sink=context.resident_output_sink,
                event_sink=context.event_sink,
                sandbox=sandbox,
                link=link,
                engine_session_key=context.resume_session_key,
                terminal_cwd=context.cwd,
                runtime_identity=context.runtime_identity,
                permission_mode=context.permission_mode,
                instructions=str(getattr(template, "system", None) or "").strip() or None,
                # From the resolved access, not guessed from a payload key: a
                # `thread/start` without a model runs Codex's own default —
                # which on a gateway that does not serve it is a 401 reading
                # like a broken adapter rather than a missing field.
                model=str(model_access.model_name or "").strip() or None,
                base_url=base_url,
                config=_engine_option(template, "config"),
                turn_start=_engine_option(template, "turn_start"),
                model_catalog=bool(catalog),
                prepare_engine_input=context.prepare_engine_input,
            )
        except BaseException:
            if link is not None:
                with contextlib.suppress(BaseException):
                    await link.close()
            raise


register_engine_adapter(ENGINE_KIND, CodexEngineAdapter())
