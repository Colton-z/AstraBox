"""EngineClient over one in-box DeepSeek Harness ``/api`` gateway.

The harness's own API, the one its browser client speaks: prompts are unary
calls; ``session/follow`` streams its durable events through ``remote.mux``.
The ``$events`` logical stream carries scoped approvals and user questions,
answered through the vendor\'s ``$events/result`` waterfall continuation. :mod:`.deepseek_harness_link` documents the wire.

Conversation identity: the harness mints the session id and the platform
persists it as ``engine_session_key``, so a reattach reuses the exact
conversation rather than authoring an id in a grammar that is the vendor's to
define. ``bind_conversation`` creates the session when there is no stored
key, and checks the stored key against the client's own on every later call.

Consumption boundary: ``session/prompt`` answers ``{"accepted": true}`` and
carries no result receipt, but echoes the request's ``requestId`` as ``rpcId`` in the
session log — the enqueued message's ``source`` is
``{"kind": "user", "rpcId": <the id this client minted>}``. That is stronger
evidence than a server-minted receipt: this client mints one id per durable
input, and the ``user/message`` event bearing it proves the engine dequeued
that exact input.
A turn also produces a second ``user/message`` whose source is a plugin's
context snapshot, so the anchor is the source id rather than the position.

``session/cancel`` ends the active turn and leaves the session usable, so
stopping a turn keeps the conversation.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from astrabox.common.utils.errors import APIError
from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    EngineConversationBinding,
    EngineInputCommand,
    EngineStreamDetached,
    EngineTurnReceipt,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness_events import (
    DeepSeekHarnessProtocolError,
    DeepSeekHarnessTurnTranslator,
    approval_response_value,
    build_approval_contract,
    build_question_contract,
    question_response_value,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness_child_runs import (
    DSH_ENGINE_KIND,
    DeepSeekHarnessChildResources,
    interrupt_dsh_child,
)
from astrabox.core.service.orchestrator.engine.emissions import (
    ChildResourceFact,
    EngineTurnEmission,
    PrivateDiagnostic,
    emission_from_translated_frame,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    input_response_message_id,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_FORM,
    PRESENTATION_TOOL_APPROVAL,
    interaction_presentation,
    is_denied_interaction_response,
)
from astrabox.core.service.orchestrator.engine.resident_relay import (
    CountedRecord,
    ResidentRelay,
)

logger = get_logger(__name__)


ENGINE_KIND = DSH_ENGINE_KIND

#: The pinned web profile's bootstrap permission presets and the command that
#: selects them. The adapter declaration requires these so its default remains
#: usable; each live Session reads the complete current list from the vendor's
#: ``permissions`` projection, including additive presets.
DSH_PERMISSION_PRESETS = ("read-only", "workspace-write", "danger-full-access")
DSH_PERMISSION_COMMAND = "/permission"

#: The harness LLM route this image's model wiring configures.
#: ``@deepseek-ai/dsh-llm-deepseek`` owns it and reads its key from
#: ``DEEPSEEK_API_KEY`` and its endpoint from ``DEEPSEEK_BASE_URL`` — the two
#: variables the adapter already declares — so it is the one route the
#: platform's gateway credential reaches. The vendor states the model id
#: passes through to the wire, which is what lets a gateway model that is not
#: in the harness's advisory catalogue be selected here.
DSH_MODEL_PROVIDER = "deepseek-official"

#: Downlink frame types this client acts on. Everything else the gateway
#: broadcasts — the projections, the queue views, host bookkeeping — is state
#: the platform holds itself and is not re-derived from here.
_FRAME_SESSION_EVENT = "session/event"
_FRAME_APPROVAL_REQUESTED = "approval/request"
_FRAME_QUESTION_REQUESTED = "user-questions/request"
#: The gateway's own report that a downlink source died. It carries no
#: sessionId — it is not about one conversation — so it must be read before
#: the session filter, or the one frame that explains a dead stream is the one
#: frame dropped.
_FRAME_STREAM_ERROR = "stream/error"


@runtime_checkable
class DeepSeekHarnessLink(Protocol):
    """One connection to one in-box harness gateway."""

    @property
    def is_live(self) -> bool: ...

    async def call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        rpc_id: str | None = None,
    ) -> Any: ...

    async def respond(self, rpc_id: str, result: dict[str, Any]) -> bool: ...

    def iter_frames(self) -> AsyncIterator[dict[str, Any]]:
        """Downlink frames in arrival order, until the socket ends."""
        ...

    async def close(self) -> None: ...


async def create_harness_session(
    link: Any, *, cwd: str | None, session_create: dict[str, Any] | None
) -> str:
    """Create one harness conversation and return the id it answers under.

    The configuration is a property of the Agent, which lets a prepared box
    create its conversation before
    any Session claims it: the claim then names this id and rejoins rather than
    creating a second one. Shared with the client so the conversation a claim
    joins is composed exactly like the one a cold start makes.
    """

    payload = dict(session_create or {})
    conflicts = set(payload) & {"cwd", "workspaceId", "sessionId"}
    if conflicts:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"DSH session_create contains platform-managed fields: {sorted(conflicts)}",
            status_code=400,
        )
    if cwd:
        payload["cwd"] = cwd
    created = await link.call("session/create", {"args": {"request": payload}})
    native = str((created or {}).get("sessionId") or "").strip()
    if not native:
        raise RuntimeError("deepseek_harness session/create returned no sessionId")
    return native


class DeepSeekHarnessEngineClient:
    """Per-conversation client over one injected :class:`DeepSeekHarnessLink`."""

    def __init__(
        self,
        *,
        session_id: str,
        link: DeepSeekHarnessLink,
        native_session_id: str | None = None,
        cwd: str | None = None,
        session_create: dict[str, Any] | None = None,
        resident_output_sink: Any = None,
        event_sink: Any = None,
    ) -> None:
        self._session_id = session_id
        self._link = link
        self._resident_output_sink = resident_output_sink
        self._event_sink = event_sink
        #: The one reader of the downlink, started with the first turn and
        #: stopped with the client; ``_translate_stream`` drains its inbox.
        self._relay: ResidentRelay | None = None
        self._inbound_sequence = 0
        self._native_session_id = str(native_session_id or "").strip() or None
        self._cwd = str(cwd or "").strip() or None
        self._session_create = dict(session_create or {})
        #: command_id → receipt: deliver() is idempotent per durable command.
        self._receipts: dict[str, EngineTurnReceipt] = {}
        #: the rpcId minted for a command's prompt → that command. The harness
        #: echoes the id into the session log, which is how a dequeue is
        #: attributed to the exact input that caused it.
        self._prompted: dict[str, EngineInputCommand] = {}
        #: The turn currently streaming, for a continuation segment to re-enter.
        self._active_receipt: EngineTurnReceipt | None = None
        #: interaction_id (the downlink frame's rpcId) → the session it belongs
        #: to. Only a process that observed the request holds this; an answer
        #: arriving after a restart is routed from the durable record instead.
        self._pending_interactions: dict[str, dict[str, Any]] = {}
        self._answered_interaction_ids: set[str] = set()
        #: Translation state for the engine turn, which outlives a platform
        #: stream segment: an interaction parks the turn by breaking out of
        #: iter_turn_events, and the answer re-enters it. A translator rebuilt
        #: per call would lose the vendor's open blocks and would wait forever
        #: for a consumption boundary that was already crossed.
        self._turn_translator: DeepSeekHarnessTurnTranslator | None = None
        self._consumption_seen = False
        self._held_back: list[dict[str, Any]] = []
        self._frames: AsyncIterator[dict[str, Any]] | None = None
        self._child_resources: DeepSeekHarnessChildResources | None = None
        #: The last failure the harness reported for this conversation out of
        #: band. The durable `turn/end` settles the turn; this is kept so a
        #: downlink that ends without one still names the cause.
        self._last_agent_error: str | None = None

    # ── EngineClient (mandatory) ─────────────────────────────────────────
    @property
    def active_receipt(self) -> EngineTurnReceipt | None:
        """The turn a continuation segment re-enters after an interaction."""

        return self._active_receipt

    @property
    def is_live(self) -> bool:
        return bool(self._link.is_live)

    @property
    def engine_session_key(self) -> str | None:
        return self._native_session_id

    async def bind_conversation(
        self,
        binding: EngineConversationBinding,
    ) -> None:
        if binding.platform_session_id != self._session_id:
            raise RuntimeError(
                "harness conversation identity mismatch: "
                f"client={self._session_id!r} "
                f"binding={binding.platform_session_id!r}"
            )
        durable_key = str(binding.engine_session_key or "").strip() or None
        if durable_key is not None:
            if (
                self._native_session_id is not None
                and durable_key != self._native_session_id
            ):
                raise RuntimeError(
                    "harness resume key does not match the configured "
                    f"conversation: configured={self._native_session_id!r} "
                    f"durable={durable_key!r}"
                )
            # The box kept running; the conversation is rejoined by naming it.
            self._native_session_id = durable_key
            return
        if self._native_session_id is not None:
            return
        self._native_session_id = await create_harness_session(
            self._link, cwd=self._cwd, session_create=self._session_create
        )

    async def deliver(self, command: EngineInputCommand) -> None:
        await self._submit(command)

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> EngineTurnReceipt:
        if consumption_confirmed:
            # Durable recovery evidence: the harness already dequeued this
            # input. Re-prompting would run the same input twice; the receipt
            # is rebuilt from the durable command identity instead.
            receipt = self._receipts.get(command.command_id) or self._receipt(
                command, prompt_rpc_id=None, input_consumed=True
            )
            self._receipts[command.command_id] = receipt
            self._begin_engine_turn(receipt, consumption_confirmed=True)
            self._ensure_relay().platform_turn_reattached()
            return receipt
        return await self._submit(command)

    async def _submit(self, command: EngineInputCommand) -> EngineTurnReceipt:
        existing = self._receipts.get(command.command_id)
        if existing is not None:
            return existing
        # One minted id per durable input, so the echo in the session log
        # identifies this exact input and a resend cannot be mistaken for it.
        prompt_rpc_id = f"astrabox-input:{command.input_id}:{uuid.uuid4().hex[:8]}"
        relay = self._ensure_relay()
        # Marked before the prompt is written: its acceptance and the turn's
        # `turn/start` are two frames on one socket, and the relay may take
        # the second before this coroutine takes the first.
        relay.platform_input_submitted()
        try:
            value = await self._link.call(
                "session/prompt",
                {"args": {"request": {
                    "requestId": prompt_rpc_id,
                    "sessionId": self._require_session(),
                    # "queue" is the harness's own word for an ordinary
                    # delivery; "steer" splices into a running turn and is
                    # not what a platform FIFO input is.
                    "mode": "queue",
                    "content": [{"type": "text", "text": command.content}],
                }}},
                rpc_id=prompt_rpc_id,
            )
        except BaseException:
            relay.platform_input_rejected()
            raise
        if not isinstance(value, dict) or value.get("accepted") is not True:
            relay.platform_input_rejected()
            raise EngineStreamDetached(
                "deepseek_harness session/prompt was not accepted "
                f"(session={self._session_id})"
            )
        self._prompted[prompt_rpc_id] = command
        receipt = self._receipt(command, prompt_rpc_id=prompt_rpc_id)
        self._receipts[command.command_id] = receipt
        self._begin_engine_turn(receipt)
        return receipt

    def _begin_engine_turn(
        self,
        receipt: EngineTurnReceipt,
        *,
        consumption_confirmed: bool = False,
    ) -> None:
        """Start one engine turn's translation state.

        Called where a turn begins — never from ``iter_turn_events``, which a
        parked turn's continuation calls a second time for the same turn.
        """

        self._active_receipt = receipt
        self._turn_translator = DeepSeekHarnessTurnTranslator(
            session_id=self._require_session()
        )
        # Durable recovery evidence that the input was already dequeued: the
        # boundary event is in the past and will not be echoed again.
        self._consumption_seen = consumption_confirmed
        self._held_back = []
        self._answered_interaction_ids.clear()
        # Scoped to the turn: an earlier turn's failure must not be offered as
        # the account of this one's.
        self._last_agent_error = None

    def _receipt(
        self,
        command: EngineInputCommand,
        *,
        prompt_rpc_id: str | None,
        input_consumed: bool = False,
    ) -> EngineTurnReceipt:
        return EngineTurnReceipt(
            # The minted prompt id is the engine-side handle for this turn; a
            # recovery rebuild without one falls back to the durable input id.
            engine_turn_id=prompt_rpc_id or command.input_id,
            engine_session_key=self._require_session(),
            started_at_monotonic_ns=time.monotonic_ns(),
            input_id=command.input_id,
            input_consumed=input_consumed,
        )

    async def iter_turn_events(
        self, receipt: EngineTurnReceipt
    ) -> AsyncIterator[EngineTurnEmission]:
        _ = receipt
        async for frame in self._translate_stream():
            emission = emission_from_translated_frame(frame)
            frame_type = str(frame.get("type") or "")
            if frame_type == "result":
                # Settle BEFORE the terminal frame is yielded. The consumer
                # stops iterating as soon as it has that frame, which closes
                # this generator — anything after the yield never runs, and
                # the turn slot would stay held, so the SECOND message in the
                # conversation comes back "already has an active turn". Only a
                # second turn shows it, which is why one-turn probes and the
                # live e2e both missed it here, in `hermes_client`, and in
                # `pi_client` before them.
                self._active_receipt = None
            yield emission
            if frame_type == "result":
                return
            if frame_type == "interaction.request":
                # The turn parks here. The engine is still running it; the
                # platform persists the record, ends this segment, and
                # re-enters after the answer — so the receipt is NOT released.
                return
        # The downlink ended without a terminal. Nothing this client does ends
        # it deliberately — cancel is a native call and settles through
        # turn/end — so every such end is a detached stream for the recovery
        # machinery to classify. Any failure the harness reported out of band
        # rides along: it is the only account of the cause when the stream
        # stopped instead of settling.
        raise self._detached(
            f"deepseek_harness downlink ended before turn/end "
            f"(session={self._session_id})"
        )

    async def cancel_turn(self, receipt: EngineTurnReceipt) -> bool:
        _ = receipt
        return await self.interrupt_active_turn()

    async def interrupt_active_turn(self) -> bool:
        """Stop the active turn through the harness's own cancel.

        The session outlives it: the harness settles the turn with
        ``turn/end`` reason ``aborted`` and stays ready for the next prompt,
        so stopping a turn keeps the conversation.
        """

        if self._native_session_id is None:
            return False
        value = await self._link.call(
            "session/cancel", {"args": {"request": {"sessionId": self._native_session_id}}}
        )
        return bool(isinstance(value, dict) and value.get("accepted") is True)

    async def stop_child_run(self, control_id: str) -> None:
        """Interrupt one continuable child through its private DSH address."""

        await interrupt_dsh_child(self._link.call, control_id)

    async def reconcile_child_resources(
        self,
    ) -> list[ChildResourceFact | PrivateDiagnostic]:
        """Refresh the vendor catalog and return typed private Session facts."""

        emissions: list[ChildResourceFact | PrivateDiagnostic] = []
        for frame in await self._child_resource_projector().refresh():
            emission = emission_from_translated_frame(frame)
            if not isinstance(emission, (ChildResourceFact, PrivateDiagnostic)):
                raise TypeError(
                    "deepseek_harness child reconcile emitted a non-private fact: "
                    f"{type(emission).__name__}"
                )
            emissions.append(emission)
        return emissions

    # ── EnginePermissionModes ────────────────────────────────────────────
    async def set_permission_mode(self, mode: str) -> None:
        """Select one of the harness's own permission presets.

        The harness bundles its two mechanism knobs, the sandbox mode and the
        approval policy, into named presets, and states the intended surface:
        "UI adapters may
        expose the table as one selector, while sandbox execution and approval
        continue to consume their own knobs"
        (``@deepseek-ai/dsh-permission-presets``). AstraBox's flat mode list
        is therefore this engine's own shape. Selecting one preset moves all
        of its knobs: ``danger-full-access`` emits ``permission/preset``, then
        ``sandbox/mode``, then ``approval/policy: never``.

        Its API exposes no setter; the preset is switched by the harness's own
        ``/permission`` command, which is a first-class endpoint of the
        gateway (``commands/execute``) rather than prompt text.
        """

        value = await self._link.call(
            "commands/execute",
            {
                "args": {
                    "agentId": self._require_session(),
                    "line": f"{DSH_PERMISSION_COMMAND} {mode}",
                    # The native descriptor requires submittedAttachments even
                    # for commands with no attachments, such as /permission.
                    "submittedAttachments": [],
                }
            },
        )
        result = (value or {}).get("result") if isinstance(value, dict) else None
        kind = str((result or {}).get("kind") or "") if isinstance(result, dict) else ""
        if kind != "success":
            # The command endpoint answers ok for a line it did not recognise,
            # so the envelope is not evidence — the command's own verdict is.
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"deepseek_harness refused permission preset {mode!r}: "
                    f"{json.dumps(value)[:200]}"
                ),
                status_code=502,
            )
        logger.info(
            "deepseek_harness permission preset set session=%s mode=%s reply=%s",
            self._session_id,
            mode,
            str((result or {}).get("text") or ""),
        )

    async def select_model(self, model: str) -> None:
        """Install the platform's model choice on this conversation.

        ``session/create`` takes only workspace, cwd, session identity and the
        Agent preset — no model — so an engine session always starts on the
        deployment's own default, which for this image is the DeepSeek
        adapter's advertised fast model. Selecting is a separate Remote, and
        without it every turn requests that default no matter which model the
        Agent names.

        Session-local and asserted on every publish, start and reattach alike:
        the vendor installs the selection for the session's next request, so
        re-asserting is how a model changed on the Agent reaches a
        conversation that already exists.

        The reply is the vendor's normalized selection, and it is checked. The
        route validates the pair itself and refuses an unknown provider with
        ``session/model-unavailable``, but a normalization that silently
        landed on another model would otherwise look like success here and
        show up as the wrong model's answers.
        """

        native = self._require_session()
        value = await self._link.call(
            "session/selectModel",
            {
                "args": {
                    "request": {
                        "sessionId": native,
                        "provider": DSH_MODEL_PROVIDER,
                        "model": model,
                    }
                }
            },
        )
        selected = (value or {}).get("selected") if isinstance(value, dict) else None
        if not isinstance(selected, dict):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"deepseek_harness returned no model selection for {model!r}: "
                    f"{json.dumps(value)[:200]}"
                ),
                status_code=502,
            )
        installed = str(selected.get("model") or "")
        provider = str(selected.get("provider") or "")
        if installed != model or provider != DSH_MODEL_PROVIDER:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    "deepseek_harness installed a different model than the "
                    f"platform selected: asked {DSH_MODEL_PROVIDER}/{model!r}, "
                    f"installed {provider}/{installed!r}"
                ),
                status_code=502,
            )
        logger.info(
            "deepseek_harness model selected session=%s provider=%s model=%s",
            self._session_id,
            provider,
            installed,
        )

    async def get_capabilities(self) -> EngineCapabilityManifest:
        history = await self._link.call(
            "session/follow",
            {"args": {"request": {"address": {
                "kind": "session", "sessionId": self._require_session(),
            }}}},
        )
        projections = history.get("projections") if isinstance(history, dict) else None
        values = projections.get("values") if isinstance(projections, dict) else None
        permissions = values.get("permissions") if isinstance(values, dict) else None
        options = permissions.get("options") if isinstance(permissions, dict) else None
        if not isinstance(options, list):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    "deepseek_harness session history has no permissions projection"
                ),
                status_code=502,
            )
        permission_modes: list[str] = []
        for index, option in enumerate(options):
            value = (
                str(option.get("value") or "").strip()
                if isinstance(option, dict)
                else ""
            )
            if not value:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message=(
                        "deepseek_harness permissions projection has an invalid "
                        f"option at index {index}"
                    ),
                    status_code=502,
                )
            # ``custom`` describes a current combination which is not one of
            # the vendor's switchable presets.
            if value == "custom":
                continue
            if value in permission_modes:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message=(
                        "deepseek_harness permissions projection repeats preset "
                        f"{value!r}"
                    ),
                    status_code=502,
                )
            permission_modes.append(value)
        if not permission_modes:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="deepseek_harness permissions projection has no presets",
                status_code=502,
            )
        return EngineCapabilityManifest(
            engine_kind=ENGINE_KIND,
            # Observed on the wire: the harness writes with `write` and
            # names its input `file_path`/`content`.
            tools=[],
            permission_modes=permission_modes,
            supports_interaction=True,
            supports_child_run_control=True,
            extra={"transport": "dsh_apiproxy_over_opensandbox_endpoint"},
        )

    async def close(self) -> None:
        relay = self._relay
        self._relay = None
        if relay is not None:
            await relay.stop()
        await self._link.close()

    # ── EngineInteractions ───────────────────────────────────────────────
    async def submit_interaction_response(
        self,
        receipt: EngineTurnReceipt,
        *,
        pending: dict[str, Any],
        response: dict[str, Any],
    ) -> bool:
        """Encode one browser answer as the harness's own client-response.

        The interaction id is read off the durable record, never off this
        process's observed state: a host that restarted between the question
        and the answer holds no memory of the request, and the harness is
        still waiting for it.
        """

        _ = receipt
        interaction_id = str(pending.get("interaction_id") or "").strip()
        if not interaction_id:
            return False
        presentation = interaction_presentation(pending)
        session_id = str(
            (pending.get("raw_input") or {}).get("sessionId") or ""
        ).strip() or self._native_session_id
        if not session_id:
            return False

        if presentation == PRESENTATION_TOOL_APPROVAL:
            result = {
                "ok": True,
                "value": approval_response_value(
                    pending,
                    session_id=session_id,
                    denied=is_denied_interaction_response(pending, response),
                ),
            }
        elif presentation == PRESENTATION_FORM:
            if is_denied_interaction_response(pending, response):
                # The harness's own cancel path for a question: its
                # ask_user_question raises ASK_CANCELLED, which the agent sees
                # as the user declining rather than as a malformed answer.
                result = {
                    "ok": False,
                    "error": {
                        "name": "UserQuestionError",
                        "code": "cancelled",
                        "message": "the user declined the question",
                        "details": {},
                    },
                }
            else:
                result = {
                    "ok": True,
                    "value": question_response_value(
                        pending, response, session_id=session_id
                    ),
                }
        else:
            logger.warning(
                "deepseek_harness cannot answer presentation=%s", presentation
            )
            return False

        accepted = await self._link.respond(interaction_id, result)
        if accepted:
            self._answered_interaction_ids.add(interaction_id)
            self._pending_interactions.pop(interaction_id, None)
        return accepted

    # ── stream translation ───────────────────────────────────────────────
    def _require_session(self) -> str:
        if self._native_session_id is None:
            raise EngineStreamDetached(
                "deepseek_harness client is not bound to a conversation "
                f"(session={self._session_id})"
            )
        return self._native_session_id

    def _detached(self, message: str) -> EngineStreamDetached:
        """A detached-stream failure that carries the cause when one was told.

        The harness reports live failures out of band and the durable
        ``turn/end`` normally settles the turn. When the downlink stops
        instead, that report is the only account of why, and a bare transport
        message would send the platform into recovery with nothing to explain.
        """

        if self._last_agent_error:
            message = f"{message}: last reported failure: {self._last_agent_error}"
        return EngineStreamDetached(message)

    def _child_resource_projector(self) -> DeepSeekHarnessChildResources:
        projector = self._child_resources
        if projector is None:
            projector = DeepSeekHarnessChildResources(
                root_session_id=self._require_session(),
                call=self._link.call,
            )
            self._child_resources = projector
        return projector

    @staticmethod
    def _echoed_text(event: dict[str, Any]) -> str:
        content = (event.get("data") or {}).get("content")
        if not isinstance(content, list):
            return ""
        return "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )

    def _consumed_frame(
        self, command: EngineInputCommand, event: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "type": "data-input-consumed",
            "id": f"input-consumed:{command.input_id}",
            "transient": True,
            "data": {
                "inputId": command.input_id,
                "responseMessageId": input_response_message_id(command.input_id),
                # The vendor-echoed text, not the submitted text: identity is
                # the boundary judgement and content is evidence of what the
                # engine actually consumed.
                "content": self._echoed_text(event),
            },
        }

    def _consumption_command(
        self, event: dict[str, Any]
    ) -> EngineInputCommand | None:
        """The input this ``user/message`` proves the engine dequeued.

        The harness carries the caller's own prompt ``rpcId`` on the message's
        ``source``. A source of any other kind — a plugin's context snapshot,
        a tool's injected message — is not an input this platform delivered.
        """

        source = (event.get("data") or {}).get("source")
        if not isinstance(source, dict) or source.get("kind") != "user":
            return None
        return self._prompted.get(str(source.get("rpcId") or "").strip())

    def _interaction_frame(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        interaction_id = str(frame.get("eventId") or "").strip()
        payload = dict(frame.get("request") or {})
        payload["sessionId"] = frame.get("agentId")
        if not interaction_id or interaction_id in self._answered_interaction_ids:
            return None
        if frame["event"] == _FRAME_APPROVAL_REQUESTED:
            contract = build_approval_contract(payload)
        else:
            contract = build_question_contract(payload)
        self._pending_interactions[interaction_id] = dict(payload)
        return {
            "type": "interaction.request",
            "interactionId": interaction_id,
            "payload": contract,
        }

    async def _translate_stream(self) -> AsyncIterator[dict[str, Any]]:
        translator = self._turn_translator
        if translator is None:
            raise EngineStreamDetached(
                "deepseek_harness turn stream entered before a turn began "
                f"(session={self._session_id})"
            )
        native = self._require_session()
        child_resources = self._child_resource_projector()
        for child_frame in await child_resources.refresh():
            if self._consumption_seen:
                yield child_frame
            else:
                self._held_back.append(child_frame)
        relay = self._ensure_relay()
        try:
            while True:
                item = await relay.turn_inbox.get()
                if isinstance(item, BaseException):
                    raise item
                frame = item.record
                frame_type = str(frame.get("type") or "")
                frame_session_id = str(
                    frame.get("payload", {}).get("sessionId") or ""
                )
                if frame_type == "waterfall" and frame.get("event") in (
                    _FRAME_APPROVAL_REQUESTED, _FRAME_QUESTION_REQUESTED,
                ):
                    frame_session_id = str(frame.get("agentId") or "")
                    if frame_session_id == native or child_resources.contains(
                        frame_session_id
                    ):
                        request = self._interaction_frame(frame)
                        if request is not None:
                            if self._consumption_seen:
                                yield request
                            else:
                                self._held_back.append(request)
                    continue
                child_owned, child_frames = await child_resources.observe_mux_frame(frame)
                # This turn persists the facts directly; only idle relay reads
                # need their native observations carried through the journal.
                child_resources.native_records()
                if child_owned:
                    for child_frame in child_frames:
                        if self._consumption_seen:
                            yield child_frame
                        else:
                            self._held_back.append(child_frame)
                    continue
                if frame_session_id != native:
                    continue
                if frame_type in {
                    "session/assistant-stream", "session/assistant-stream-snapshot",
                }:
                    for translated in self._translate_output_frame(translator, frame):
                        if self._consumption_seen:
                            yield translated
                        else:
                            self._held_back.append(translated)
                    continue
                if frame_type != _FRAME_SESSION_EVENT:
                    continue
                event = frame.get("payload", {}).get("event")
                if not isinstance(event, dict):
                    continue
                if (
                    not self._consumption_seen
                    and str(event.get("type") or "") == "user/message"
                ):
                    command = self._consumption_command(event)
                    if command is not None:
                        self._consumption_seen = True
                        yield self._consumed_frame(command, event)
                        for held in self._held_back:
                            yield held
                        self._held_back.clear()
                        continue
                for translated in self._translate_output_frame(translator, frame):
                    if not self._consumption_seen:
                        # Nothing may precede the consumption boundary. These
                        # frames are held rather than dropped: the harness opens a
                        # turn with turn/start and step/start before it echoes the
                        # prompt, so they belong to this turn and are released in
                        # order once the boundary is crossed. No terminal reaches
                        # here — an earlier turn's is dropped above, and this
                        # turn's cannot precede its own echo.
                        self._held_back.append(translated)
                        continue
                    yield translated
                if translator.terminal_seen:
                    return
        except EngineStreamDetached as detached:
            # The link raises this when the socket ends, so a reported cause is
            # attached here rather than at the fall-through below, which a real
            # link never reaches. Nothing reported means nothing to add, and
            # the original failure travels on untouched.
            if not self._last_agent_error:
                raise
            raise self._detached(str(detached)) from detached

    # ── the relay ────────────────────────────────────────────────────────
    def _ensure_relay(self) -> ResidentRelay:
        relay = self._relay
        if relay is None:
            relay = ResidentRelay(
                seam=_DshRelaySeam(self),
                session_id=self._session_id,
                engine_session_key=self._require_session(),
                next_record=self._next_inbound,
                send_command=self._send_relay_command,
                current_sequence=self._current_inbound_sequence,
                resident_output_sink=self._resident_output_sink,
                event_sink=self._event_sink,
            )
            self._relay = relay
            relay.start()
        failure = relay.failure
        if failure is not None:
            # The one reader of this downlink is gone, so nothing the platform
            # writes now would ever be answered: the link is detached.
            raise self._detached(
                f"deepseek_harness downlink reader stopped (session={self._session_id}): {failure}"
            ) from failure
        return relay

    async def _next_inbound(self) -> CountedRecord:
        """The next downlink frame, numbered; the wire's own death raised here.

        Two frames are about the downlink rather than about a session, so they
        are read where the wire is read, before any routing by session.
        """

        if self._frames is None:
            self._frames = self._link.iter_frames()
        frame = await self._frames.__anext__()
        self._inbound_sequence += 1
        frame_type = str(frame.get("type") or "")
        if frame_type == _FRAME_STREAM_ERROR:
            error = frame.get("payload", {}).get("error")
            detail = ""
            if isinstance(error, dict):
                detail = f"{error.get('code')}: {error.get('message')}"
            raise EngineStreamDetached(
                "deepseek_harness downlink reported a stream error "
                f"(session={self._session_id}): {detail or 'no detail'}"
            )
        if frame_type == "emit" and frame.get("event") == "api-session/error":
            args = frame.get("args") or []
            if len(args) == 2 and args[0] == self._native_session_id:
                # Not a detached stream. The vendor declares this event as "a
                # step or turn errored ... even when the error has no in-turn
                # position", relays it in its own client as the session's last
                # agent error, and keeps the session live. Every turn-scoped
                # failure is also appended durably as `turn/end` with reason
                # kind `error`, which is what settles the turn — and the emit
                # arrives FIRST, so raising here replaced the vendor's own
                # message with a lost connection and sent the platform into
                # recovery. A bad model name surfaced that way as "the live
                # engine client does not implement output reconnect".
                self._last_agent_error = str(args[1])
                logger.warning(
                    "deepseek_harness reported a live failure session=%s: %s",
                    self._session_id,
                    self._last_agent_error,
                )
        return CountedRecord(sequence=self._inbound_sequence, record=frame)

    async def _current_inbound_sequence(self) -> int:
        return self._inbound_sequence

    def _translate_output_frame(
        self, translator: DeepSeekHarnessTurnTranslator, frame: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Translate durable and process-local supplier output on the same path."""

        payload = frame.get("payload")
        if not isinstance(payload, dict):
            return []
        if payload.get("sessionId") != self._require_session():
            return []
        frame_type = frame.get("type")
        if frame_type == "session/assistant-stream":
            return list(translator.translate_assistant_stream(payload["frame"]))
        if frame_type == "session/assistant-stream-snapshot":
            return list(translator.restore_assistant_stream(payload["baseline"]))
        if frame_type == _FRAME_SESSION_EVENT:
            return list(translator.translate(payload["event"]))
        return []

    async def _send_relay_command(self, payload: dict[str, Any]) -> Any:
        raise RuntimeError(
            "deepseek_harness owes the relay no child reads; the harness pushes "
            f"child state on the mux (session={self._session_id}, payload={payload!r})"
        )


class _DshRelaySeam:
    """The harness's answers to the relay, in its own downlink vocabulary.

    The gateway multiplexes every session's events onto one downlink, so a
    run is a turn on the conversation's own session: `turn/start` there opens
    it, `turn/end` there is the vendor's end, and the identity is the turn
    index the harness numbers them with. The link has no positions — a
    session event's `seq` is per session, and the mux frames of other sessions
    carry none — so the sequence is the client's own count. A child session's
    frames fold through the child projector rather than translating. The
    projector retains native catalog context and followed records for the
    idle relay to journal and the adapter to replay without a live box.
    Approval and question requests on the conversation's session or
    on a child's are interactions.
    """

    engine_kind = ENGINE_KIND

    def __init__(self, client: DeepSeekHarnessEngineClient) -> None:
        self._client = client

    @staticmethod
    def sequence(wire: CountedRecord) -> int:
        return wire.sequence

    @staticmethod
    def record(wire: CountedRecord) -> dict[str, Any]:
        return wire.record

    def _own_event(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """The session event, when the record is one on the conversation's session."""

        if str(record.get("type") or "") != _FRAME_SESSION_EVENT:
            return None
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        if str(payload.get("sessionId") or "") != self._client._require_session():
            return None
        event = payload.get("event")
        return event if isinstance(event, dict) else None

    def starts_run(self, record: dict[str, Any]) -> bool:
        event = self._own_event(record)
        return event is not None and str(event.get("type") or "") == "turn/start"

    def settles_run(self, record: dict[str, Any]) -> bool:
        event = self._own_event(record)
        return event is not None and str(event.get("type") or "") == "turn/end"

    def response_id(self, record: dict[str, Any], sequence: int) -> str:
        event = self._own_event(record) or {}
        data = event.get("data")
        turn = data.get("turn") if isinstance(data, dict) else None
        if turn is None:
            raise DeepSeekHarnessProtocolError(
                "deepseek_harness turn/start carries no turn index"
            )
        return f"{self._client._require_session()}:{turn}"

    def new_translator(self) -> DeepSeekHarnessTurnTranslator:
        return DeepSeekHarnessTurnTranslator(session_id=self._client._require_session())

    def translate(
        self, translator: DeepSeekHarnessTurnTranslator, record: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return self._client._translate_output_frame(translator, record)

    async def child_facts(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        projector = self._client._child_resource_projector()
        owned, frames = await projector.observe_mux_frame(record)
        return list(frames) if owned else []

    def native_records(self) -> list[dict[str, Any]]:
        return self._client._child_resource_projector().native_records()

    def owed_child_reads(self) -> list[dict[str, Any]]:
        return []

    def carries_child_facts(self, record: dict[str, Any]) -> bool:
        return self._client._child_resource_projector().may_own(record)

    def interaction(self, record: dict[str, Any]) -> dict[str, Any] | None:
        if str(record.get("type") or "") != "waterfall" or record.get("event") not in (
            _FRAME_APPROVAL_REQUESTED,
            _FRAME_QUESTION_REQUESTED,
        ):
            return None
        client = self._client
        session_id = str(record.get("agentId") or "")
        if session_id != client._require_session() and not (
            client._child_resource_projector().contains(session_id)
        ):
            return None
        return client._interaction_frame(record)
