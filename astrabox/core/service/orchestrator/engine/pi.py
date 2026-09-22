"""Pi engine adapter (engine_kind="pi").

Pi (``earendil-works/pi``, an open-source terminal coding agent) ships a
headless surface — ``pi --mode rpc`` — that speaks JSON lines on stdin and
stdout. AstraBox runs one such process per conversation behind the sandbox's
execd pipe, so pi opens no port and no platform credential is copied into the
box.

Three properties of pi shape this adapter, and all three are the vendor's:

* **No permission system.** Pi says so itself and recommends containment
  instead. The sandbox already is that containment, so this engine declares
  no permission modes rather than inventing controls that change nothing.
* **The session is a file.** Pi persists a conversation under its session
  directory and resumes it by id, which is what makes a reattach possible
  after the process — or this host — has gone away.
* **Models are the vendor's own layer.** Pi reaches any OpenAI-compatible
  endpoint through a provider declared in ``models.json``, so the deployment's
  model gateway needs no shim: the image renders that file at boot from the
  base URL the platform hands it, and pi interpolates the credential itself
  from the environment.

Self-registers as engine_kind="pi" on import.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import shlex
import time
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
    CONVERSATION_PLACEMENT_PER_ACCOUNT,
    EngineRuntimeCapabilities,
    EngineWorkloadDeclaration,
    EngineSessionLogDeclaration,
)
from astrabox.core.service.orchestrator.engine.pi_client import (
    ENGINE_KIND,
    PiEngineClient,
)
from astrabox.core.service.orchestrator.engine.provisioning import (
    ENGINE_ENV_FILE_NAME,
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
from astrabox.core.service.orchestrator.runtime.sandbox_script_writer import (
    install_verified_text_script,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import EXECD_PORT

logger = get_logger(__name__)

#: The pi image's own boot script: it renders pi's provider configuration and
#: then execs the base image's init. Required and explicit — the backend's
#: ``None`` default is the Claude agent image's boot script, which this image
#: does not contain, so the container would exit 127 at boot.
PI_IMAGE_ENTRYPOINT = ("/opt/astrabox/boot.sh",)
PI_RUNTIME_IMAGE_COMPONENT = "sandbox-pi"

#: The variable pi's ``models.json`` interpolates the model credential from.
#: Pi resolves ``"$VAR"`` in that file's ``apiKey`` itself, so the credential
#: never lands on disk — under the vault this carries a placeholder and the
#: egress sidecar injects the real value.
PI_CREDENTIAL_ENV_VAR = "ASTRABOX_PI_API_KEY"

#: AstraBox variables the image reads at boot. ``models.json`` only accepts a
#: literal ``baseUrl`` — pi interpolates the environment for ``apiKey`` and
#: ``headers`` and nothing else — so the image renders the file from these
#: when the container starts. That is deployment-level input, present before
#: any session claims the box, which is what keeps a prewarmed box valid.
PI_BASE_URL_ENV_VAR = "ASTRABOX_PI_BASE_URL"
PI_MODEL_ENV_VAR = "ASTRABOX_PI_MODEL"
PI_WORKSPACE_ENV_VAR = "ASTRABOX_WORKSPACE"

#: The provider name the image writes into ``models.json``. Pi addresses a
#: model as ``provider/id``, so this is half of the model reference and is
#: fixed by the image and this adapter together.
PI_PROVIDER_NAME = "astrabox"

#: Where the image's boot renderer puts that file
#: (``containers/sandbox-pi/render-models-config.sh``). Named on both sides of
#: one box: the image writes it, and slot preparation reads it back as the
#: proof that a box created ahead of its Session can hold a conversation.
PI_MODELS_CONFIG_PATH = f"{SANDBOX_IMAGE_WORKLOAD_HOME}/.pi/agent/models.json"

#: How long slot preparation waits for that file before destroying the box.
#: The renderer is backgrounded behind the base image's init
#: (``containers/sandbox-pi/boot.sh``) and then waits up to 30 s for the
#: workload account that init creates, so a box reaching Running proves nothing
#: about its provider table; this bound is that wait plus a margin.
PI_MODELS_CONFIG_TIMEOUT_SECONDS = 45.0
PI_MODELS_CONFIG_POLL_SECONDS = 0.25

#: Where pi keeps sessions. Naming it explicitly is what makes a reattach
#: possible: the conversation is a file, and resuming names that file's id.
PI_SESSION_DIR_TEMPLATE = "{home}/.pi/sessions"
PI_SUBAGENT_TEMP_ROOT_TEMPLATE = "{home}/.pi/subagents"

#: The namespace pi's session scopes take in the platform's transcript store.
#: Named here and in the image that installs the relay, on opposite sides of
#: one box.
PI_MIRROR_SUBPATH_PREFIX = "pi/"

#: Where the image installs the sub-agent package, and the settings key Pi
#: discovers it through. Pi ships no sub-agents and points at packages for
#: them, so this is the capability, and like every other capability it belongs
#: to the image. It reaches Pi through the settings file the launch writes
#: because that write REPLACES the file: a copy placed in the image would be
#: overwritten before Pi ever read it, which is exactly what happened.
PI_SUBAGENT_PACKAGE_PATH = "/opt/pi/node_modules/pi-subagents"
PI_PACKAGES_SETTING = "packages"

PI_ENGINE_OPTIONS_SCHEMA: tuple[dict[str, Any], ...] = (
    {
        "key": "settings",
        "label": "Pi settings.json",
        "type": "object",
        "protected_keys": ["defaultProvider", "defaultModel", "sessionDir"],
        "help": (
            "Replaces the root of the conversation's ~/.pi/agent/settings.json "
            "before Pi starts. Inner keys pass through unchanged; Pi applies "
            "its native defaults and trusted project overrides. Use "
            "defaultThinkingLevel for thinking. Model/provider selection "
            "and sessionDir are platform-owned CLI arguments. "
            "https://github.com/earendil-works/pi/blob/v0.85.1/packages/coding-agent/docs/settings.md"
        ),
    },
)

#: Where an AstraBox Agent's instructions reach this engine. Pi discovers
#: ``AGENTS.md`` in the working directory on its own — the vendor's extension
#: point, and the counterpart of Claude Code's ``CLAUDE.md``. Using it is the
#: difference between an Agent whose instructions take effect and a field that
#: looks configurable and is silently dropped.
PI_AGENT_INSTRUCTIONS_FILE = "AGENTS.md"


def _subagent_temp_root(home: str) -> str:
    """The same official package root for the native process and its reader."""
    return PI_SUBAGENT_TEMP_ROOT_TEMPLATE.format(home=home.rstrip("/"))


def build_pi_rpc_command(
    *,
    linux_user: str,
    home: str,
    workspace: str,
    model: str,
    settings: dict[str, Any] | None = None,
    resume_session_key: str | None = None,
    engine_env_file: str | None = None,
    isolated: bool = False,
) -> str:
    """Build the secret-free command execd starts for one conversation.

    The command carries paths, the workload account and the model reference.
    The credential is not here: pi reads it through ``models.json``, which
    interpolates it from the environment at request time. On the shared
    tenancy that environment is the conversation's engine-env file
    (``engine_env_file``), sourced before the child starts. An isolated execd
    already runs as the assigned account; a box-level execd must drop to the
    image workload account before starting pi.

    ``resume_session_key`` names an existing pi session file, which is how a
    reattach continues the same conversation rather than starting a blank one
    in a box that still remembers it.

    ``settings`` replaces the conversation-local global settings file as the
    workload user before startup. Empty settings clear the previous Agent
    configuration; Pi owns project merging and interpretation of inner keys.
    """

    values = {
        "linux_user": str(linux_user or "").strip(),
        "home": str(home or "").strip(),
        "workspace": str(workspace or "").strip(),
        "model": str(model or "").strip(),
    }
    if any(not value for value in values.values()):
        missing = sorted(key for key, value in values.items() if not value)
        raise ValueError(f"pi launcher is missing: {', '.join(missing)}")

    argv = [
        "pi",
        "--mode",
        "rpc",
        "--provider",
        PI_PROVIDER_NAME,
        "--model",
        values["model"],
        "--session-dir",
        # Each conversation's private home also contains its native session
        # files; a sandbox-wide path would mix shared-tenancy histories.
        PI_SESSION_DIR_TEMPLATE.format(home=values["home"].rstrip("/")),
    ]
    resume = str(resume_session_key or "").strip()
    if resume:
        argv += ["--session", resume]

    inner = "\n".join(
        (
            "set -euo pipefail",
            f"test \"$(id -un)\" = {shlex.quote(values['linux_user'])}",
            # Missing file = loud stop under `set -e`, which is right: a
            # shared-tenancy child without its engine env would answer every
            # request with an empty credential instead.
            *(
                [f". {shlex.quote(engine_env_file)}"]
                if engine_env_file
                else []
            ),
            f"export HOME={shlex.quote(values['home'])}",
            f"export USER={shlex.quote(values['linux_user'])}",
            f"export LOGNAME={shlex.quote(values['linux_user'])}",
            f"export PI_SUBAGENTS_TEMP_ROOT={shlex.quote(_subagent_temp_root(values['home']))}",
            'mkdir -p "$PI_CODING_AGENT_DIR"',
            "printf '%s\\n' "
            + shlex.quote(json.dumps(settings or {}, ensure_ascii=False))
            + ' > "$PI_CODING_AGENT_DIR/settings.json"',
            f"cd {shlex.quote(values['workspace'])}",
            "exec " + " ".join(shlex.quote(part) for part in argv),
        )
    )
    return " ".join(
        (
            *(
                ["exec env -u EXECD_ACCESS_TOKEN"]
                if isolated
                else ["exec runuser -u", shlex.quote(values["linux_user"]), "-- env"]
            ),
            f"HOME={shlex.quote(values['home'])}",
            f"USER={shlex.quote(values['linux_user'])}",
            f"LOGNAME={shlex.quote(values['linux_user'])}",
            # Rendered over THIS conversation's home for the same reason as
            # --session-dir above: the image pins the box-level value to the
            # image account's home, and env is the vendor's knob for it. On
            # the conversation tenancy the rendering is byte-identical to
            # that pin.
            f"PI_CODING_AGENT_DIR={shlex.quote(values['home'].rstrip('/') + '/.pi/agent')}",
            "bash --noprofile --norc -c",
            shlex.quote(inner),
        )
    )


def _shared_engine_env_file(
    runtime_identity: dict[str, Any] | None,
) -> str | None:
    """The conversation's engine-env file, on the tenancy that has one."""

    identity = dict(runtime_identity or {})
    if str(identity.get("sandbox_tenancy") or "").strip() != "agent":
        return None
    home = str(identity.get("home_dir") or "").strip().rstrip("/")
    if not home:
        raise ValueError(
            "an agent-tenancy pi conversation carries no home_dir; the "
            "engine-env file has nowhere to be"
        )
    return f"{home}/{ENGINE_ENV_FILE_NAME}"


def _conversation_execd_port(runtime_identity: dict[str, Any] | None) -> int:
    """Resolve the assigned conversation's pipe service using platform placement."""

    identity = dict(runtime_identity or {})
    if str(identity.get("sandbox_tenancy") or "").strip() != "agent":
        return EXECD_PORT
    uid = identity.get("uid")
    if uid is None:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                "an agent-tenancy pi conversation carries no uid; its "
                "per-conversation execd port cannot be derived"
            ),
            status_code=500,
        )
    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        runner_port_for_uid,
    )

    return runner_port_for_uid(int(uid))


def _settings(template: Any) -> dict[str, Any]:
    """The Agent's native Pi settings, carrying the image's own package.

    Vendor keys are not interpreted. The one addition is the sub-agent package
    the image installs: an Agent may declare packages of its own, and this
    appends rather than replaces so naming one cannot silently take the
    engine's child runs away with it.
    """

    bag = getattr(template, "engine_options", None)
    settings = dict(bag.get("settings") or {}) if isinstance(bag, dict) else {}
    declared = settings.get(PI_PACKAGES_SETTING)
    packages = list(declared) if isinstance(declared, list) else []
    if PI_SUBAGENT_PACKAGE_PATH not in packages:
        packages.append(PI_SUBAGENT_PACKAGE_PATH)
    settings[PI_PACKAGES_SETTING] = packages
    return settings


async def _park_pi_child(
    sandbox: Any,
    template: Any,
    *,
    cwd: str,
    model: str,
    linux_user: str | None = None,
    home: str | None = None,
    engine_env_file: str | None = None,
    runtime_identity: dict[str, Any] | None = None,
    service_credential: str | None = None,
) -> str:
    """Start pi in the prepared box and prove it answers; return its pipe id.

    Preparation creates the execd pipe, boots the child, and waits for the
    first ``get_state`` it can answer, a measured ten-second sequence.
    Every input is Agent-level — the model, the thinking level, the working
    directory and the instructions file are the same for every conversation
    this Agent will ever hold — so freezing them here is what makes the child
    reusable by whichever Session claims the unit, and what puts them in the
    slot's spawn fingerprint.

    A parked child that cannot answer is not parked: the caller destroys the
    box rather than publishing a unit whose engine is already unusable.
    """

    from astrabox.core.service.orchestrator.engine.pi_client import (
        connect_pi_client,
    )

    client = await connect_pi_client(
        sandbox,
        platform_session_id="",
        cwd=cwd,
        session_root=PI_SESSION_DIR_TEMPLATE.format(home=home or SANDBOX_IMAGE_WORKLOAD_HOME),
        subagent_temp_root=_subagent_temp_root(home or SANDBOX_IMAGE_WORKLOAD_HOME),
        port=_conversation_execd_port(runtime_identity),
        service_credential=service_credential,
        command=build_pi_rpc_command(
            linux_user=linux_user or SANDBOX_IMAGE_WORKLOAD_USER,
            home=home or SANDBOX_IMAGE_WORKLOAD_HOME,
            workspace=cwd,
            model=model,
            settings=_settings(template),
            resume_session_key=None,
            engine_env_file=engine_env_file,
            isolated=(runtime_identity or {}).get("sandbox_tenancy") == "agent",
        ),
        resume_session_key=None,
    )
    try:
        pty_session_id = await client.park_process()
    except BaseException:
        with contextlib.suppress(BaseException):
            await client.close()
        raise
    # The pipe outlives this client object; closing the client here would take
    # the child with it. Only the transport is released.
    await client.release_without_stopping()
    return pty_session_id


async def _install_agent_instructions(
    sandbox: Any, *, cwd: str, instructions: str
) -> None:
    """Place the Agent's instructions where pi already looks.

    Written into the conversation's own workspace before the process starts,
    so pi loads them with its first prompt. A pooled box is not a problem: the
    workspace is per-conversation and the platform creates it at claim time —
    this is content in it, not a capability assembled into the box.
    """

    await install_verified_text_script(
        sandbox,
        path=f"{cwd.rstrip('/')}/{PI_AGENT_INSTRUCTIONS_FILE}",
        content=instructions if instructions.endswith("\n") else instructions + "\n",
        mode=0o644,
        error_code="AGENT_RUNTIME_ERROR",
        error_message="failed to install the pi agent instructions",
    )


def _engine_sandbox_request(
    model_access: Any, *, model: str
) -> EngineSandboxRequest:
    """What a box holding one pi conversation must be. One declaration.

    Read by both creates — the Session's own, and a prepared slot's — because
    the two must produce the same box. The drift this prevents is invisible
    until a turn: the image renders ``models.json`` from this environment at
    boot, so a prepared box built from a second, slightly different
    declaration would answer a claim with a provider table naming another
    gateway or another model.
    """

    return EngineSandboxRequest(
        entrypoint=PI_IMAGE_ENTRYPOINT,
        credential=ModelCredentialRequest(
            access=model_access,
            # Pi's openai-completions provider posts to
            # {base}/chat/completions, which is exactly what the
            # sidecar is told to admit.
            request_paths=("chat/completions",),
            missing_code="ENGINE_CAPABILITY_UNAVAILABLE",
            missing_message=(
                "the pi environment resolves no model gateway "
                "credential/base URL; configure the environment's "
                "model access"
            ),
        ),
        credential_env_var=PI_CREDENTIAL_ENV_VAR,
        isolated_service_auth=True,
        cwd_env_var=PI_WORKSPACE_ENV_VAR,
        env={
            PI_BASE_URL_ENV_VAR: str(model_access.base_url or "").strip(),
            PI_MODEL_ENV_VAR: model,
        },
    )


def _slot_spawn_fingerprint(template: Any, *, base_url: str, model: str) -> str:
    """The box-frozen facts a prepared pi box answers for, as one digest.

    Covers exactly what the box CREATE fixes and a later spawn cannot
    override: the image and its entrypoint, and the gateway base URL and model
    the image renders into ``models.json`` at boot — a box carries one
    provider table with one model in it, so both rotate the box rather than
    the process. The workload identity contract is here too because the
    workspace and account paths the create pre-renders come from it.

    Spawn-borne facts are deliberately absent. ``AGENTS.md`` (the Agent's
    system prompt), ``settings.json`` and the working directory are all read by
    ``pi --mode rpc`` at startup, and this engine's prepared box holds no
    started process — the claim writes and passes them — so changing any of
    them must NOT retire a warm box. The claim recomputes this digest from the
    current Agent and discards a box whose value differs; the Agent-level
    preparation fingerprint cannot answer for these, because it covers pool
    inputs and not the engine's own gateway resolution.
    """

    payload = {
        "engine_kind": ENGINE_KIND,
        "image": resolve_runtime_template_name(template),
        "entrypoint": list(PI_IMAGE_ENTRYPOINT),
        "base_url": str(base_url or ""),
        "model": str(model or ""),
        "provider": PI_PROVIDER_NAME,
        "workload_user": SANDBOX_IMAGE_WORKLOAD_USER,
        "workload_home": SANDBOX_IMAGE_WORKLOAD_HOME,
        "workspace": SANDBOX_IMAGE_WORKSPACE_DIR,
        "subagent_temp_root_template": PI_SUBAGENT_TEMP_ROOT_TEMPLATE,
    }
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _rendered_models(raw: str) -> set[str] | None:
    """The model ids a box's provider table publishes for this adapter.

    None means the text is not a pi provider table naming this adapter's
    provider at all — a different answer from "names other models", and the
    caller waits on the first while refusing the second.
    """

    try:
        document = json.loads(raw)
    except ValueError:
        return None
    providers = document.get("providers") if isinstance(document, dict) else None
    provider = providers.get(PI_PROVIDER_NAME) if isinstance(providers, dict) else None
    entries = provider.get("models") if isinstance(provider, dict) else None
    if not isinstance(entries, list):
        return None
    return {
        str(entry.get("id") or "").strip()
        for entry in entries
        if isinstance(entry, dict) and str(entry.get("id") or "").strip()
    }


async def _await_rendered_models_config(sandbox: Any, *, model: str) -> None:
    """Refuse a prepared box until pi has the provider table it will need.

    This is the barrier a prepared pi box has instead of a resident service to
    probe. Pi reaches its gateway through ``models.json`` and nothing else, so
    a box whose renderer never finished answers its first turn with
    ``Model provider 'astrabox' not found`` — inside a box a Session has
    already claimed, which is the failure this whole preparation contract
    exists to move earlier. Preparation is the one moment it can be turned
    into a discarded slot.

    Read back rather than merely waited for: the image renders exactly the one
    model ``ASTRABOX_PI_MODEL`` names, so proving the table names THIS model
    turns the fingerprint's claim about the box's frozen model into an
    observation of it.
    """

    deadline = time.monotonic() + PI_MODELS_CONFIG_TIMEOUT_SECONDS
    detail = "the file did not exist"
    while True:
        raw = ""
        try:
            raw = str(await sandbox.files.read_file(PI_MODELS_CONFIG_PATH) or "")
        except Exception as exc:  # noqa: BLE001 - the absent file reads as an error
            detail = f"{type(exc).__name__}: {exc}"
        if raw.strip():
            models = _rendered_models(raw)
            if models is None:
                detail = (
                    f"it holds no {PI_PROVIDER_NAME!r} provider with a model list"
                )
            elif model not in models:
                # Waiting cannot fix this: the renderer runs once at boot from
                # environment the create fixed, and a claim has no channel to
                # correct it.
                raise APIError(
                    code="AGENT_PREWARM_CONFIG_INVALID",
                    message=(
                        f"the prepared pi box publishes {sorted(models)!r} under "
                        f"provider {PI_PROVIDER_NAME!r}, but this Agent resolves "
                        f"model {model!r}; pi would refuse every turn in it"
                    ),
                    status_code=409,
                )
            else:
                return
        if time.monotonic() >= deadline:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"the prepared pi box never rendered {PI_MODELS_CONFIG_PATH} "
                    f"within {PI_MODELS_CONFIG_TIMEOUT_SECONDS:.0f}s ({detail}); "
                    "pi reaches its model gateway through that file alone"
                ),
                status_code=502,
            )
        await asyncio.sleep(PI_MODELS_CONFIG_POLL_SECONDS)


def _model_reference(model_access: Any) -> str:
    """The model id pi is started with, taken from the resolved access.

    Read from the seam's own resolution rather than from the Agent's
    ``model_config`` bag: the platform already merges the Agent's model, the
    Environment's default and the deployment's, and re-deriving it here would
    be a second answer that disagrees with the credential and base URL beside
    it.
    """

    model = str(getattr(model_access, "model_name", "") or "").strip()
    if not model:
        raise APIError(
            code="ENGINE_CAPABILITY_UNAVAILABLE",
            message=(
                "the pi environment resolves no model; configure the "
                "environment's model access"
            ),
            status_code=409,
        )
    return model


async def _publish_runtime(
    *,
    session_id: str,
    sandbox: Any,
    client: PiEngineClient,
    engine_session_key: str | None,
    terminal_cwd: str,
    runtime_identity: dict[str, Any] | None = None,
    prepare_engine_input: Any = None,
) -> SessionRuntime:
    """One live client → one conversation-bound, published SessionRuntime."""

    engine_manifest = await initialize_engine_client(
        client,
        expected_engine_kind=ENGINE_KIND,
        conversation_binding=EngineConversationBinding(
            platform_session_id=session_id,
            engine_session_key=engine_session_key,
        ),
    )
    return SessionRuntime(
        session_id=session_id,
        agent=None,
        engine_kind=ENGINE_KIND,
        engine_client=client,
        engine_manifest=engine_manifest,
        conversation_bound=True,
        sandbox=sandbox,
        sandbox_id=extract_sandbox_id(sandbox),
        # Under the Agent-shared tenancy the box is shared; the isolated
        # session is what teardown closes, what a reattach rebuilds from, and
        # half of the consumption receipts' carrier identity.
        isolated_session_id=(
            str((runtime_identity or {}).get("isolated_session_id") or "")
            or None
        ),
        engine_session_key=client.engine_session_key,
        terminal_cwd=terminal_cwd,
        # Pi has no permission-mode concept, so there is none to reconcile.
        # Publishing one would make the platform gate dispatch on a control
        # the engine never reads.
        permission_mode=None,
        runtime_identity=runtime_identity,
        prepare_engine_input=prepare_engine_input,
    )


class PiEngineAdapter(EngineAdapter):
    """EngineAdapter for pi's RPC mode over the sandbox's execd pipe."""

    def child_run_is_active(self, child_run: dict[str, Any]) -> bool:
        from astrabox.core.service.orchestrator.engine.child_runs import (
            ChildRunProjectionError,
        )
        from astrabox.core.service.orchestrator.engine.pi_child_runs import (
            _SNAPSHOT_STATES,
        )

        if child_run.get("closed") is True:
            return False
        state = child_run.get("engine_status")
        if state not in _SNAPSHOT_STATES:
            raise ChildRunProjectionError(f"Pi child has unknown state {state!r}")
        # pi-subagents' isActiveAsyncState excludes paused, retained runs.
        return state in {"queued", "running"}

    @property
    def engine_kind(self) -> EngineKind:
        return ENGINE_KIND

    @property
    def engine_client_type(self) -> type[EngineClient]:
        return PiEngineClient

    def durable_child_resource_facts(
        self,
        raw_messages: list[dict[str, Any]],
    ) -> list[tuple[int, ChildResourceFact]]:
        """Child facts from pi's native records the platform persisted.

        These are the records the relay journals while no run is open: a
        sub-agent status push and the replies to the reads it owed. Folded
        in journal order by a fresh projector, so a child that changed state
        while nothing was running is visible on the next read.
        """

        from astrabox.core.service.orchestrator.engine.pi_child_runs import (
            PiChildResources,
            is_async_status_widget,
            is_inspect_widget,
        )

        resources = PiChildResources()
        facts: list[tuple[int, ChildResourceFact]] = []
        for index, record in enumerate(raw_messages):
            if not isinstance(record, dict):
                continue
            if is_async_status_widget(record):
                frames = resources.observe_ui_request(record)
            elif is_inspect_widget(record):
                frames = resources.observe_persisted_inspect_reply(record)
            else:
                frames = resources.observe_tool_event(record)
            for frame in frames:
                emission = emission_from_translated_frame(frame)
                if isinstance(emission, ChildResourceFact):
                    facts.append((index, emission))
        return facts

    def stored_child_transcript_facts(
        self, *, engine_ref: str, closed: bool,
        raw_scopes: list[dict[str, Any]], raw_messages: list[dict[str, Any]],
    ) -> list[ChildResourceFact]:
        from astrabox.core.service.orchestrator.engine.pi_child_transcript import (
            stored_child_transcript_facts,
        )

        return stored_child_transcript_facts(
            engine_ref=engine_ref, closed=closed,
            raw_scopes=raw_scopes, raw_messages=raw_messages,
        )

    @property
    def capabilities(self) -> EngineRuntimeCapabilities:
        return EngineRuntimeCapabilities(
            engine_kind=ENGINE_KIND,
            supported_session_kinds=frozenset({"agent_chat"}),
            workload=EngineWorkloadDeclaration(
                # Pi's own config directory name, from its package
                # manifest (``piConfig.configDir``).
                config_dir_name=".pi",
                required_commands=(
                    "bash",
                    "pi",
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
            default_runtime_image=release_image(PI_RUNTIME_IMAGE_COMPONENT),
            # Pi writes each conversation to one append-only JSONL file under
            # its session directory and offers none of it over the RPC surface,
            # so the box is the only place that conversation exists and a
            # replacement one exits with `No session found matching '<id>'`.
            # Declaring the directory is the whole of this adapter's part: the
            # platform relays the bytes out while the box lives and writes them
            # back before anything asks pi to resume.
            session_log=EngineSessionLogDeclaration(
                root_template=PI_SESSION_DIR_TEMPLATE,
                namespace=PI_MIRROR_SUBPATH_PREFIX,
            ),
            engine_options_schema=PI_ENGINE_OPTIONS_SCHEMA,
            configuration_inputs=frozenset(),
            # Pi ships no permission system — its own documentation says so
            # and recommends containment instead. An empty declaration is the
            # honest one; the platform then has nothing to validate and must
            # not invent a vocabulary for it.
            permission_modes=(),
            permission_mode_defaults=(),
        )

    # Pi's prepared unit holds one initialized, parked child. Conversation
    # tenancy places it in a whole box; Agent tenancy places the same single-use
    # child in an isolated account inside the shared box. ``pi --mode rpc`` owns
    # one conversation on one process's stdin/stdout in either placement. This
    # flag declares that the whole-box form is supported as well; it does not
    # exclude the shared-slot form.
    #
    # The child is safe to park only because preparation marks its transcript
    # mirror target as deliberately unclaimed for longer than the slot TTL.
    # Preparation installs every fact pi freezes at startup — ``AGENTS.md``,
    # ``--model``, ``settings.json`` and the working directory — before starting
    # it. Those facts are in the preparation fingerprint, so an Agent change
    # retires the box instead of asking claim to repair a process that already
    # read them. Claim binds the Session mirror target and credential before it
    # adopts the parked pipe.
    prepares_conversation_box = True

    async def prepare_runtime(
        self, context: EnginePreparationContext
    ) -> dict[str, Any]:
        """Prepare pi inside a placement already owned by the platform.

        Preparation here is the box and its barrier. An engine with a resident
        server proves that server; pi has none between turns, so what has to be
        proven is the one thing the image builds asynchronously and the whole
        conversation depends on — the rendered ``models.json``. A box that
        misses it is destroyed rather than published, because the alternative
        is a Session claiming a box in which pi refuses every turn.

        The engine proves its rendered provider table and parks its vendor
        process. It never creates, connects, claims or destroys a box.
        """

        target_slot = str(context.slot_id or "").strip()
        if not target_slot:
            raise APIError(
                code="AGENT_PREWARM_CONFIG_INVALID",
                message="prepared pi box requires a slot id",
                status_code=500,
            )
        template = context.template
        identity = context.runtime_identity
        model_access = context.model_access
        model = _model_reference(model_access)
        base_url = str(getattr(model_access, "base_url", "") or "").strip()
        if not base_url:
            # The cold path lets the image's renderer fail loud in the boot
            # log, which a Session start observes. Nothing observes a prepared
            # box's boot, so this refusal is where that evidence has to be.
            raise APIError(
                code="ENGINE_CAPABILITY_UNAVAILABLE",
                message=(
                    "the pi environment resolves no model gateway base URL, so "
                    "a prepared box would render no provider table and could "
                    "never hold a conversation"
                ),
                status_code=409,
            )
        await _await_rendered_models_config(context.sandbox, model=model)
        source_cwd = (
            str(identity.get("workspace_source_dir") or "").strip()
            if context.placement == "shared_slot"
            else context.cwd
        )
        instructions = str(getattr(template, "system", None) or "").strip()
        if instructions and source_cwd:
            await _install_agent_instructions(
                context.sandbox,
                cwd=source_cwd,
                instructions=instructions,
            )
        parked_pty_session_id = await _park_pi_child(
            context.sandbox,
            template,
            cwd=context.cwd,
            runtime_identity=identity,
            service_credential=context.service_credential,
            model=model,
            linux_user=(
                str(identity.get("linux_user") or "") or None
                if context.placement == "shared_slot"
                else None
            ),
            home=(
                str(identity.get("home_dir") or "") or None
                if context.placement == "shared_slot"
                else None
            ),
            engine_env_file=(
                _shared_engine_env_file(identity)
                if context.placement == "shared_slot"
                else None
            ),
        )
        logger.info(
            "prepared pi box: slot=%s box=%s model=%s",
            target_slot,
            context.sandbox_id,
            model,
        )
        return {
            "engine_kind": ENGINE_KIND,
            "spawn_fingerprint": _slot_spawn_fingerprint(
                template, base_url=base_url, model=model
            ),
            # Only user-identity MCP servers belong in the activation set; pi
            # declares none, so the set is empty.
            "activation_mcp_servers": [],
            # The claim adopts this PTY because it holds a pi that answered
            # `get_state`; pipe creation, child boot and the proof take a
            # measured ten seconds.
            "parked_pty_session_id": parked_pty_session_id,
        }

    def sandbox_request(
        self,
        *,
        template: Any,
        model_access: Any,
    ) -> EngineSandboxRequest:
        """Declare pi's image and RPC process prerequisites."""

        model = _model_reference(model_access)
        return _engine_sandbox_request(model_access, model=model)

    async def activate_runtime(
        self,
        context: EngineStartupContext,
    ) -> SessionRuntime:
        """Start or adopt pi's RPC child inside an assigned runtime."""

        if context.attach_mode is not None and not str(
            context.resume_session_key or ""
        ).strip():
            raise APIError(
                code="ENGINE_RUNTIME_UNAVAILABLE",
                message=(
                    f"pi session {context.session_id!r} carries no engine session "
                    "key to rejoin. Start a new conversation."
                ),
                status_code=409,
            )
        template = context.template
        model = _model_reference(context.model_access)
        prepared = context.prepared_manifest or {}
        is_prepared = context.prepared_manifest is not None
        if str(prepared.get("placement") or "") == "conversation_box":
            expected_spawn = _slot_spawn_fingerprint(
                template,
                base_url=str(getattr(context.model_access, "base_url", "") or ""),
                model=model,
            )
            if expected_spawn != str(prepared.get("spawn_fingerprint") or ""):
                raise APIError(
                    code="AGENT_PREWARM_CONFIG_INVALID",
                    message=(
                        "the prepared pi box was built from a different image, "
                        "gateway, model, or instruction set"
                    ),
                    status_code=409,
                )
        parked_pty_session_id = str(
            prepared.get("parked_pty_session_id") or ""
        ).strip()
        if is_prepared and not parked_pty_session_id:
            raise APIError(
                code="AGENT_PREWARM_CONFIG_INVALID",
                message=(
                    "claimed prepared runtime names no parked pi child; a "
                    "claim adopts one and cannot create it"
                ),
                status_code=409,
            )
        sandbox = context.sandbox
        client: PiEngineClient | None = None
        try:
            instructions = str(getattr(template, "system", None) or "").strip()
            if instructions and not is_prepared:
                await _install_agent_instructions(
                    sandbox, cwd=context.cwd, instructions=instructions
                )
            identity = context.runtime_identity or {}
            # The key the PLATFORM honoured, not the one the plan asked for:
            # it dropped the plan's key when the transcript store had nothing to
            # restore, and naming it anyway would make pi exit with
            # `No session found matching '<id>'` instead of answering.
            resume_key = str(context.resume_session_key or "").strip()
            command = build_pi_rpc_command(
                linux_user=str(identity.get("linux_user") or SANDBOX_IMAGE_WORKLOAD_USER),
                home=str(identity.get("home_dir") or SANDBOX_IMAGE_WORKLOAD_HOME),
                workspace=context.cwd,
                model=model,
                settings=_settings(template),
                resume_session_key=resume_key or None,
                engine_env_file=_shared_engine_env_file(identity),
                isolated=identity.get("sandbox_tenancy") == "agent",
            )
            from astrabox.core.service.orchestrator.engine.pi_client import (
                connect_pi_client,
            )

            client = await connect_pi_client(
                sandbox,
                platform_session_id=context.session_id,
                cwd=context.cwd,
                session_root=PI_SESSION_DIR_TEMPLATE.format(
                    home=identity.get("home_dir") or SANDBOX_IMAGE_WORKLOAD_HOME
                ),
                subagent_temp_root=_subagent_temp_root(
                    str(identity.get("home_dir") or SANDBOX_IMAGE_WORKLOAD_HOME)
                ),
                port=_conversation_execd_port(identity),
                service_credential=context.service_credential,
                pty_session_id=(
                    parked_pty_session_id or None
                ),
                command=command,
                resume_session_key=resume_key or None,
                switch_prepared_session=is_prepared and bool(resume_key),
                resident_output_sink=context.resident_output_sink,
                event_sink=context.event_sink,
            )
            return await _publish_runtime(
                session_id=context.session_id,
                sandbox=sandbox,
                client=client,
                engine_session_key=resume_key or None,
                terminal_cwd=context.cwd,
                runtime_identity=context.runtime_identity,
                prepare_engine_input=context.prepare_engine_input,
            )
        except BaseException:
            if client is not None:
                with contextlib.suppress(BaseException):
                    await client.close()
            raise

    def shared_conversation_service_launch(
        self, *, home: str, workspace: str, port: int
    ) -> str:
        """Launch this conversation's mirror and execd pipe service in isolation."""

        import shlex

        # The isolated session's shell does not set the account's HOME, and
        # the script derives every per-conversation path from it, so the
        # trigger must carry it. TMPDIR points at a private-home spool for the
        # same reason claude_code's trigger does: the session's /tmp is not
        # in the isolation policy's writable roots, and the engine writes
        # os.tmpdir() at boot.
        log = f"{home}/.astrabox-pi-launch.log"
        spool = f"{home}/.astrabox-spool"
        return (
            f"mkdir -p {shlex.quote(spool)} && "
            f"HOME={shlex.quote(home)} "
            f"TMPDIR={shlex.quote(spool)} "
            f"setsid /opt/pi/serve-conversation.sh {int(port)} "
            f"</dev/null >>{shlex.quote(log)} 2>&1 &"
        )

register_engine_adapter(ENGINE_KIND, PiEngineAdapter())
