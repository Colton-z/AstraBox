"""Claude Code engine adapter — declares and activates Claude capabilities.

The platform owns allocation, workspace, credentials and startup ordering.
This adapter describes the box Claude needs and activates only Claude's vendor
protocol after the platform hands it a prepared runtime.

Self-registers as engine_kind="claude_code" on import.
"""

from typing import Any, get_args

from claude_agent_sdk import PermissionMode
from claude_agent_sdk.types import TERMINAL_TASK_STATUSES, TaskUpdatedStatus

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine import (
    claude_code_background,
    claude_code_runtime,
    claude_transcript,
)
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EngineClient,
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

from astrabox.providers.sandbox_image import resolve_agent_image


class ClaudeCodeEngineAdapter(EngineAdapter):
    """EngineAdapter for Claude Code — wraps the Claude Agent SDK remote runtime.

    The adapter declares its image contract and activates the vendor protocol
    only after the platform has prepared a placement.

    An Environment's ``runtime_template_name`` remains an explicit image pin.
    When it is empty, ``capabilities.default_runtime_image`` supplies the live
    deployment default for this adapter; changing the Claude deployment image
    therefore moves only unpinned Environments.
    """

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

    @property
    def engine_kind(self) -> EngineKind:
        return "claude_code"

    @property
    def engine_client_type(self) -> type[EngineClient]:
        from astrabox.core.service.orchestrator.engine.claude_code_client import (
            ClaudeCodeEngineClient,
        )

        return ClaudeCodeEngineClient

    @property
    def capabilities(self) -> EngineRuntimeCapabilities:
        # The translation-shell profile (docs/design-translation-shell-2026-07.md):
        # turns drive through ClaudeCodeEngineClient over the in-box runner;
        # the SDK owns turn lifecycle and recovery.
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
            # Native JSON is passed to the SDK; only platform wiring is reserved.
            engine_options_schema=CLAUDE_ENGINE_OPTIONS_SCHEMA,
            configuration_inputs=frozenset(
                # `tracing` is an Environment field, not an Agent one, but the
                # question this set answers is the same for both: does the
                # selected adapter read it. Claude Code does — the vendor's CLI
                # takes an OTLP endpoint from `OTEL_*` in its environment, which
                # this adapter writes into the box.
                {"mcp_servers", "skills", "plugin_repos", "tracing"}
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

    def durable_session_message(
        self, message: dict[str, Any]
    ) -> SessionMessageFact | None:
        from astrabox.core.service.orchestrator.engine.claude_hook_messages import (
            session_start_message,
        )

        return session_start_message(message)

    def durable_child_resource_facts(
        self,
        raw_messages: list[dict[str, Any]],
    ) -> list[tuple[int, ChildResourceFact]]:
        cursor = ClaudeStreamCursor()
        facts: list[tuple[int, ChildResourceFact]] = []
        for message_index, message in enumerate(raw_messages):
            for frame in translate_claude_sdk_message(
                message,
                envelope_seq=message_index + 1,
                cursor=cursor,
            ):
                emission = emission_from_translated_frame(frame)
                if isinstance(emission, ChildResourceFact):
                    facts.append((message_index, emission))
        return facts

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

    def detached_child_run_terminal(
        self,
        raw_event: dict[str, Any],
        *,
        transcript_refs: set[str],
        engine_refs: set[str],
        transcript_to_engine_ref: dict[str, str],
        activation_to_engine_ref: dict[str, str],
        observed_activations: dict[str, str],
        control_to_engine_ref: dict[str, str] | None = None,
    ) -> dict[str, str] | None:
        return claude_code_background.background_terminal_fact_for_manifest(
            raw_event,
            transcript_refs=transcript_refs,
            engine_refs=engine_refs,
            transcript_to_engine_ref=transcript_to_engine_ref,
            activation_to_engine_ref=activation_to_engine_ref,
            observed_activations=observed_activations,
            control_to_engine_ref=control_to_engine_ref,
        )

    def slice_recovery_turn(
        self,
        raw_items: list[dict[str, Any]],
        *,
        prompt_text: str,
    ) -> list[dict[str, Any]]:
        return claude_transcript.slice_turn_tail_entries(
            raw_items,
            prompt_text=prompt_text,
        )

    def has_transcript_terminal_evidence(
        self,
        raw_items: list[Any],
    ) -> bool:
        return claude_transcript.has_terminal_evidence(raw_items)

    def project_settled_transcript(
        self,
        raw_items: list[Any],
        *,
        done: bool = False,
    ):
        return claude_transcript.project_settled_transcript(raw_items, done=done)

    def detached_child_run_transcript_blocks(
        self,
        raw_scopes: list[dict[str, Any]],
        *,
        root_transcript_ref: str,
        engine_ref: str,
    ) -> list[dict[str, Any]]:
        return claude_code_background.background_task_transcript_tree_blocks(
            raw_scopes,
            root_transcript_ref=root_transcript_ref,
            parent_engine_ref=engine_ref,
        )

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

        spool = f"{home}/.astrabox-spool"
        log = f"{home}/.astrabox-runner.log"
        return (
            f"mkdir -p {shlex.quote(spool)} && "
            f"cd -- {shlex.quote(workspace)} && "
            f"ASTRABOX_RUNNER_SPOOL_DIR={shlex.quote(spool)} "
            f"TMPDIR={shlex.quote(spool)} "
            f"ASTRABOX_RUNNER_PORT={port} setsid "
            f"/usr/local/bin/python3.12 /opt/astrabox/sandbox_runner.py "
            f"</dev/null >>{shlex.quote(log)} 2>&1 &"
        )

register_engine_adapter("claude_code", ClaudeCodeEngineAdapter())
