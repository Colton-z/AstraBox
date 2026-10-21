"""Claude Code engine adapter — declares and activates Claude capabilities.

The platform owns allocation, workspace, credentials and startup ordering.
This adapter describes the box Claude needs and activates only Claude's vendor
protocol after the platform hands it a prepared runtime.

Self-registers as engine_kind="claude_code" on import.
"""

from typing import Any, Literal, get_args

from claude_agent_sdk import PermissionMode
from claude_agent_sdk.types import TERMINAL_TASK_STATUSES, TaskUpdatedStatus

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine import (
    claude_code_background,
    claude_code_runtime,
)
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EngineKind,
    EnginePreparationContext,
    EngineStartupContext,
    EngineStartupMaterialRequest,
)
from astrabox.core.service.orchestrator.engine.provisioning import (
    EngineSandboxRequest,
)
from astrabox.core.service.orchestrator.engine.emissions import (
    ChildResourceFact,
    SessionMessageFact,
    emission_from_translated_frame,
)
from astrabox.core.service.orchestrator.engine.frame_scope import session_scoped_engine_frame
from astrabox.core.service.orchestrator.engine.frame_translator import (
    ClaudeStreamCursor,
    translate_claude_sdk_message,
)
from astrabox.core.service.orchestrator.engine.claude_code_options import (
    CLAUDE_ENGINE_OPTIONS_SCHEMA,
)
from astrabox.core.service.orchestrator.engine.registry import (
    register_engine_adapter,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    CONVERSATION_PLACEMENT_PER_ACCOUNT,
    EngineRuntimeCapabilities,
    EngineWorkloadDeclaration,
)
from astrabox.core.service.orchestrator.engine.extensions import EngineExtensions, PackageKind

from astrabox.providers.sandbox_image import resolve_agent_image


class ClaudeCodeEngineAdapter(EngineAdapter):
    """EngineAdapter for Claude Code — wraps the Claude Agent SDK remote runtime.

    The adapter declares its image contract and activates the vendor protocol
    only after the platform has prepared a placement.

    A RuntimeProfile's ``runtime_template_name`` remains an explicit image pin.
    When it is empty, ``capabilities.default_runtime_image`` supplies the live
    deployment default for this adapter; changing the Claude deployment image
    therefore moves only unpinned RuntimeProfiles.
    """

    def startup_environment_scope(self) -> Literal["process", "session"] | None:
        return "process"

    def supports_initialized_environment(self) -> bool:
        return True

    def canonical_child_message_content(
        self, content: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        from astrabox.core.service.orchestrator.engine.claude_child_runs import (
            claude_content_blocks,
        )

        return claude_content_blocks({"content": content})

    def child_run_is_active(self, child_run: dict[str, Any]) -> bool:
        from astrabox.core.service.orchestrator.engine.child_runs import (
            ChildRunProjectionError,
        )

        if child_run.get("closed") is True:
            return False
        event = child_run.get("engine_event")
        if event == "session_store.stop_reason":
            return claude_code_background.agent_stop_reason_is_active(
                str(child_run.get("engine_reason") or "")
            )
        status = child_run.get("engine_status")
        if status in TERMINAL_TASK_STATUSES or status == "paused":
            return False
        if status in get_args(TaskUpdatedStatus):
            return True
        if status:
            raise ChildRunProjectionError(f"Claude task has unknown status {status!r}")
        # Started/progress carry no status; updated is a partial native patch.
        if event in {"task_started", "task_progress", "task_updated"}:
            return True
        raise ChildRunProjectionError(f"Claude task has no activity status for event {event!r}")

    def child_run_fact_reopens(self, data: dict[str, Any]) -> bool:
        """Revive a closed task only on a fact that changes its state.

        ``TaskProgressMessage`` is emitted while a task is in progress, and a
        ``TaskUpdatedMessage`` patch carries only the fields that changed
        (``claude_agent_sdk.types``). A progress report or a patch that names
        no status therefore leaves a terminal status standing: journaled after
        it, such a fact describes the activation that status ended.
        ``task_started`` and a patch to a live status change the state.
        """
        engine_event = str(data.get("engineEvent") or "").strip()
        if engine_event == "task_progress":
            return False
        if engine_event == "task_updated":
            return bool(str(data.get("engineStatus") or "").strip())
        return True

    def stored_child_transcript_facts(
        self,
        *,
        engine_ref: str,
        closed: bool,
        native_state: dict[str, Any],
        raw_scopes: list[dict[str, Any]],
    ) -> list[ChildResourceFact]:
        """One Agent's messages from its own transcript in the SessionStore mirror.

        Claude writes each Agent's conversation to a transcript of its own
        (``subagents/agent-<id>.jsonl``); a background Agent's messages reach
        the platform only there. Returns no facts when the mirror holds no
        transcript of this Agent. ``closed`` does not change the answer: the
        transcript is the Agent's whole conversation so far either way.
        """
        blocks = claude_code_background.background_task_transcript_tree_blocks(
            raw_scopes, root_transcript_ref=engine_ref, parent_engine_ref=engine_ref
        )
        return [
            ChildResourceFact(
                session_scoped_engine_frame({"type": "data-subagent", "id": block["id"], "data": block["data"]})
            )
            for block in blocks
            if block["data"].get("kind") == "message" and block["data"].get("engineRef") == engine_ref
        ]

    @property
    def engine_kind(self) -> EngineKind:
        return "claude_code"

    @property
    def engine_client_type(self) -> type[Any]:
        from astrabox.core.service.orchestrator.engine.claude_runtime_client import ClaudeRuntimeClient

        return ClaudeRuntimeClient

    @property
    def capabilities(self) -> EngineRuntimeCapabilities:
        # The SDK, through the in-box runner, owns the conversation and its recovery.
        return EngineRuntimeCapabilities(
            engine_kind="claude_code",
            supported_session_kinds=frozenset({"agent_chat"}),
            workload=EngineWorkloadDeclaration(
                config_dir_name=".claude",
                config_env_var="CLAUDE_CONFIG_DIR",
                required_commands=(
                    "bash",
                    "getent",
                    "runuser",
                    "id",
                    "mkdir",
                    "chown",
                    "chmod",
                    "/usr/local/bin/astrabox-provision-conversation",
                    "/usr/local/bin/astrabox-assistant-workspace-storage",
                ),
            ),
            conversation_placement=CONVERSATION_PLACEMENT_PER_ACCOUNT,
            default_runtime_image=resolve_agent_image(),
            permission_modes=tuple(get_args(PermissionMode)),
            permission_mode_defaults=(("agent_chat", "bypassPermissions"),),
            extensions=EngineExtensions(
                mcp_transports=frozenset({"stdio", "http", "sse"}),
                skills_target="{config}/skills",
                packages=(
                    PackageKind(
                        name="claude-plugin",
                        label="Claude Code plugin",
                        target="{config}/plugins",
                        help="A Claude Code plugin directory: Skills, commands, agents, hooks and MCP servers.",
                    ),
                ),
                # Native JSON is passed to the SDK; only platform wiring is reserved.
                native_config=CLAUDE_ENGINE_OPTIONS_SCHEMA,
                # The vendor's CLI takes an OTLP endpoint from `OTEL_*` in its
                # environment, which this adapter writes into the box.
                tracing=True,
            ),
        )

    def sandbox_request(
        self,
        *,
        template: Any,
        model_access: Any,
    ) -> EngineSandboxRequest:
        """Declare Claude's image and protocol prerequisites."""

        return claude_code_runtime.sandbox_request(
            template=template, model_access=model_access
        )

    def startup_material_request(
        self,
        *,
        template: Any,
        model_access: Any,
        deployment_settings: Any,
    ) -> EngineStartupMaterialRequest:
        """Ask the platform for the two scoped callbacks Claude's runner uses."""

        _ = (template, model_access, deployment_settings)
        return EngineStartupMaterialRequest(
            transcript_store=True,
            sandbox_death_notice=True,
        )


    async def activate_runtime(
        self, context: EngineStartupContext
    ) -> Any:
        """Activate Claude after platform startup has made the box ready."""

        return await claude_code_runtime.activate_runtime(context)


    async def prepare_runtime(
        self, context: EnginePreparationContext
    ) -> dict[str, Any]:
        if not str(context.runner_uri or "").strip():
            # Claude's prepared unit is a parked CLI child behind the runner's
            # prepare/activate barriers; without a runner there is nothing to
            # park it in. Both whole-box and isolated-slot preparations must
            # supply the endpoint of their already running image service.
            raise APIError(
                code="AGENT_PREWARM_UNSUPPORTED",
                message="claude slot preparation requires the slot's runner",
                status_code=500,
            )
        return await claude_code_runtime.prepare_runtime(context)


    def shared_conversation_service_launch(
        self, *, home: str, workspace: str, port: int
    ) -> str:
        """The in-box runner, as one backgrounded launch line.

        The runner spools transcript batches to disk before acking the SDK,
        and its default /tmp is root-owned inside an isolated session.
        The session's writable private home keeps this runtime state outside
        the user workspace.
        """

        import shlex

        from astrabox.core.service.orchestrator.engine.runner_link import (
            RUNNER_TOKEN_FILE_NAME,
        )

        spool = f"{home}/.astrabox-spool"
        log = f"{home}/.astrabox-runner.log"
        token_file = f"{home}/{RUNNER_TOKEN_FILE_NAME}"
        return (
            f"mkdir -p {shlex.quote(spool)} && "
            f"cd -- {shlex.quote(workspace)} && "
            f"ASTRABOX_RUNNER_SPOOL_DIR={shlex.quote(spool)} "
            f"ASTRABOX_RUNNER_TOKEN_FILE={shlex.quote(token_file)} "
            f"TMPDIR={shlex.quote(spool)} "
            f"ASTRABOX_RUNNER_PORT={port} setsid "
            f"/usr/local/bin/python3.12 /opt/astrabox/sandbox_runner.py "
            f"</dev/null >>{shlex.quote(log)} 2>&1 &"
        )

register_engine_adapter("claude_code", ClaudeCodeEngineAdapter())
