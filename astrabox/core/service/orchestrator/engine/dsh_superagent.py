"""dsh-superagent: an Assistant runtime that hosts no conversations.

One resident DeepSeek Harness process per Assistant, composed with the
dsh-superagent bundle (``containers/sandbox-dsh-superagent``). The bundle polls
its Sources (GitHub, optionally Feishu), routes what people ask in its own main
Session and runs Executor Sessions; people talk to it where they already work
and supervise it in the bundle's own panel, which the console embeds. So the
platform's part is the box: the Assistant's account and home, its model route,
the agent account's GitHub credential when it has one, custody of its state,
and a signed address for the panel.

Activation writes the Assistant's inputs into its home and publishes this box's
initialization gate; the image's programs start the harness only then
(``astrabox-dsh-superagent-serve``), so a box that exists before the Assistant
claims it holds nothing of it.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import posixpath
import re
import shlex
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.config.release_images import release_image
from astrabox.core.service.orchestrator.engine.assistant_state_barrier import (
    await_command_channel,
    close_supervised_restarts,
    confirm_forced_save,
    restart_resident_program,
    stop_supervised_program,
    wait_until_answering,
)
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EngineKind,
    EngineStartupContext,
    EngineStartupMaterialRequest,
    RuntimeStateSaveRequired,
)
from astrabox.core.service.orchestrator.assistant_a2a_auth import assistant_a2a_targets
from astrabox.core.service.orchestrator.engine.capabilities import (
    CONVERSATION_PLACEMENT_PER_ACCOUNT,
    EngineAssistantDeclaration,
    EngineOwnerSecretDeclaration,
    EnginePanelDeclaration,
    EngineRuntimeCapabilities,
    EngineWorkloadDeclaration,
)
from astrabox.core.service.orchestrator.engine.conversationless_client import (
    ConversationlessRuntimeClient,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness import (
    DSH_PROFILE_PACKAGE_OPTION,
    profile_package_hosts,
    profile_package_step,
)
from astrabox.core.service.orchestrator.engine.extensions import EngineExtensions
from astrabox.core.service.orchestrator.engine.openai_wire import resolve_openai_base_url
from astrabox.core.service.orchestrator.engine.provisioning import (
    EngineSandboxRequest,
    ModelCredentialRequest,
)
from astrabox.core.service.orchestrator.engine.registry import register_engine_adapter
from astrabox.core.service.orchestrator.engine.startup import owner_secret_action
from astrabox.core.service.orchestrator.engine.runtime import (
    RuntimeProtocolError,
    initialize_runtime_client,
)
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    normalize_runtime_identity,
)
from astrabox.core.service.orchestrator.runtime.models import SessionRuntime
from astrabox.core.service.orchestrator.runtime.sandbox_script_writer import (
    install_verified_text_script,
)
from astrabox.seams.model import ResolvedModelAccess

ENGINE_KIND = "dsh_superagent"
RUNTIME_IMAGE_COMPONENT = "sandbox-dsh-superagent"
#: The in-box port the browser hop listens on (``panel-hop.conf``).
PANEL_PORT = 44790
#: The owner secret the bundle's GitHub Source signs its agent account in with.
GITHUB_TOKEN = "github_token"
#: The deployment's shared agent account token, by SecretProvider name
#: (``ASTRABOX_DSH_SUPERAGENT_GITHUB_TOKEN``), used by every Assistant whose owner
#: sets none of their own. The bundle reads each repository's notifications by
#: repository and never marks them read, so Assistants sharing one account do
#: not take each other's requests.
GITHUB_TOKEN_DEPLOYMENT_SECRET = "astrabox-dsh-superagent-github-token"

_ENV_FILENAME = "astrabox-dsh-superagent.env"
_TARGET_FILENAME = "astrabox-runtime-state.json"
_DIGEST_FILENAME = "astrabox-dsh-superagent-process.sha256"
#: The Agents the owner can dispatch Tickets to, with each one's A2A ingress
#: URL and bearer (``assistant_a2a_targets``). The image's A2A executor reads
#: it whenever it dispatches, so it is written on every activation without a
#: restart.
_AGENTS_FILENAME = "astrabox-a2a-agents.json"
_STATE_SCRIPT = "/opt/astrabox/superagent/superagent_state.py"
_HARNESS_PROGRAM = "astrabox-dsh-superagent"
_STATE_PROGRAM = "astrabox-dsh-superagent-state"
_SUPERVISE_LOCK = "/run/astrabox/dsh-superagent-supervise.lock"
_PAUSING_MARKER = "/run/astrabox/dsh-superagent-pausing"
_PROBE = "/usr/local/bin/astrabox-dsh-superagent-probe"
_GH_LOGIN = "/usr/local/bin/astrabox-dsh-superagent-gh-login"
#: The harness migrates the bundle's database and composes its profile before
#: the panel answers; a cold box also starts the image's other services.
_ANSWER_BUDGET_SECONDS = 300.0
#: Installs the owner's plugin Bundles into the harness's web profile with the
#: harness's own plugin command, before the harness starts or restarts, and
#: links the image's bundle into the profile: a Source plugin in its own
#: package imports `dsh-superagent/sources` from the bundle the harness runs.
#: The profile's package files are not part of the saved state
#: (superagent_state.py), so every box installs them from the configuration.
_PROFILE_PACKAGES = r"""set -euo pipefail
dsh_home="$DSH_HOME"
@STEP@
mkdir -p "$profile/node_modules"
ln -sfn /opt/dsh-superagent "$profile/node_modules/dsh-superagent"
printf 'DSH_SUPERAGENT_PACKAGES_READY\n'
"""
_INSTALL_BUDGET_SECONDS = 600
_SAVE_MARKER = re.compile(r"^DSH_SUPERAGENT_STATE_SAVE_COMPLETE snapshot_id=\S+$", re.M)


class DshSuperagentRuntimeClient(ConversationlessRuntimeClient):
    engine_kind = ENGINE_KIND


def _gateway_base_url(model_access: ResolvedModelAccess) -> str:
    return resolve_openai_base_url(
        model_access, engine_label="dsh-superagent", error_code="MODEL_ENDPOINT_INVALID"
    )


def _probe_foreign(answer: str) -> RuntimeProtocolError:
    return RuntimeProtocolError(
        f"dsh-superagent panel answered {answer!r} through its browser hop"
    )


def _probe_unready(exit_code: int) -> APIError:
    return APIError(
        code="SANDBOX_INBOX_SERVER_UNREADY",
        message=(
            "dsh-superagent panel did not answer through its browser hop "
            f"(probe exit {exit_code}; 124 means its budget ran out)"
        ),
        status_code=502,
    )


def _credential_request(model_access: ResolvedModelAccess) -> ModelCredentialRequest:
    """The model route the bundle's Sessions use, on the OpenAI wire pi-ai speaks."""

    return ModelCredentialRequest(
        access=replace(
            model_access,
            base_url=_gateway_base_url(model_access),
        ),
        request_paths=("chat/completions",),
        missing_code="AGENT_RUNTIME_ERROR",
        missing_message="model api key not configured for the dsh-superagent Assistant",
        missing_status=500,
    )


def _env_exports(env: dict[str, str]) -> str:
    lines = ["# Generated by AstraBox. Mode 0600; used only by this Assistant's harness."]
    for key, value in sorted(env.items()):
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"invalid dsh-superagent profile env key: {key!r}",
                status_code=500,
            )
        lines.append(f"export {key}={shlex.quote(str(value))}")
    return "\n".join(lines) + "\n"


class DshSuperagentEngineAdapter(EngineAdapter):
    @property
    def engine_kind(self) -> EngineKind:
        return ENGINE_KIND

    @property
    def engine_client_type(self) -> type[Any]:
        return DshSuperagentRuntimeClient

    @property
    def capabilities(self) -> EngineRuntimeCapabilities:
        return EngineRuntimeCapabilities(
            engine_kind=ENGINE_KIND,
            supported_session_kinds=frozenset({"assistant_chat"}),
            workload=EngineWorkloadDeclaration(
                config_dir_name=".dsh",
                config_env_var="DSH_HOME",
                required_commands=(
                    "bash",
                    "getent",
                    "runuser",
                    "id",
                    "mkdir",
                    "chown",
                    "chmod",
                    "gh",
                    "/usr/local/bin/gh-agent",
                    "/usr/local/bin/astrabox-dsh-superagent-serve",
                    "/usr/local/bin/astrabox-provision-conversation",
                    "/usr/local/bin/astrabox-provision-assistant-profile",
                ),
            ),
            conversation_placement=CONVERSATION_PLACEMENT_PER_ACCOUNT,
            default_runtime_image=release_image(RUNTIME_IMAGE_COMPONENT),
            # The harness takes plugin Bundles in its web profile, as a
            # DeepSeek Harness Agent does; a Source plugin is one.
            extensions=EngineExtensions(native_config=(DSH_PROFILE_PACKAGE_OPTION,)),
            assistant=EngineAssistantDeclaration(
                conversations=False,
                panel=EnginePanelDeclaration(port=PANEL_PORT),
                owner_secrets=(
                    EngineOwnerSecretDeclaration(
                        key=GITHUB_TOKEN,
                        label="GitHub agent account token",
                        help=(
                            "A classic personal access token of the GitHub account the agent "
                            "acts as, with the repo and notifications scopes. The agent "
                            "comments, pushes and opens pull requests as this account. "
                            "Leave it unset to use the deployment's shared agent account, "
                            "and invite that account to the repository instead. With "
                            "neither, the agent has no GitHub account and works only from "
                            "its other Sources."
                        ),
                        deployment_secret=GITHUB_TOKEN_DEPLOYMENT_SECRET,
                        required=False,
                    ),
                ),
            ),
        )

    def sandbox_request(self, *, template: Any, model_access: Any) -> EngineSandboxRequest:
        return EngineSandboxRequest(
            entrypoint=("/opt/gem/run.sh",),
            credential=_credential_request(model_access),
            credential_env_var=None,
            cwd="/home/conversations",
            env={},
            required_network_hosts=(
                "github.com",
                "api.github.com",
                "codeload.github.com",
                "objects.githubusercontent.com",
            ),
        )

    def install_egress_hosts(self, engine_options: dict[str, Any]) -> list[str]:
        """The hosts pnpm reaches for the profile package's dependencies."""

        return profile_package_hosts(engine_options)

    def startup_material_request(
        self, *, template: Any, model_access: Any, deployment_settings: Any
    ) -> EngineStartupMaterialRequest:
        _ = template, model_access, deployment_settings
        return EngineStartupMaterialRequest(secret_names=(), runtime_state_store=True)

    async def activate_runtime(self, context: EngineStartupContext) -> SessionRuntime:
        """Hand the Assistant's inputs to the box, open its gate, and wait for the panel."""

        sandbox = context.sandbox
        identity = normalize_runtime_identity(context.runtime_identity)
        if not identity:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="platform startup supplied no Assistant runtime identity",
                status_code=500,
            )
        if not context.runtime_state_store:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="platform startup supplied no dsh-superagent runtime-state target",
                status_code=500,
            )
        # Without a token the agent has no GitHub account: the bundle's GitHub Source
        # stays unset and its other Sources run (upstream-changes.patch).
        token = str(context.owner_secrets.get(GITHUB_TOKEN) or "").strip()
        model_name = str(getattr(context.model_access, "model_name", "") or "").strip()
        credential = str(context.model_credential or "").strip()
        if not model_name or not credential:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="the dsh-superagent Assistant has no model or model credential",
                status_code=500,
            )
        commands = sandbox.commands
        await await_command_channel(sandbox, engine_label="dsh-superagent")
        config_dir = identity["config_dir"]
        env = {
            **dict(context.runtime_env or {}),
            "DSH_HOME": config_dir,
            "ASTRABOX_DSH_GATEWAY_BASE_URL": _gateway_base_url(context.model_access),
            "ASTRABOX_DSH_MODEL": model_name,
            "DEEPSEEK_API_KEY": credential,
            "ASTRABOX_DSH_PROFILE_LINUX_USER": identity["linux_user"],
            "ASTRABOX_DSH_PROFILE_HOME": identity["home_dir"],
            "ASTRABOX_DSH_WORKSPACE": identity["workspace_dir"],
            "DSH_SUPERAGENT_A2A_AGENTS_FILE": posixpath.join(config_dir, _AGENTS_FILENAME),
        }
        env_path = posixpath.join(config_dir, _ENV_FILENAME)
        env_content = _env_exports(env)
        options = getattr(context.template, "engine_options", None)
        packages = (options or {}).get("profile_package") if isinstance(options, dict) else None
        packages = dict(packages) if isinstance(packages, dict) else {}
        # The harness reads its environment and its Bundles when it starts, so
        # a change to either takes effect by a restart.
        digest = hashlib.sha256(
            (env_content + json.dumps(packages, sort_keys=True)).encode("utf-8")
        ).hexdigest()
        digest_path = posixpath.join(config_dir, _DIGEST_FILENAME)
        previous = await commands.run(
            "cat " + shlex.quote(digest_path) + " 2>/dev/null || true", timeout=30
        )
        changed = previous.output.strip() not in ("", digest)

        async def install(path: str, content: str, what: str) -> None:
            await install_verified_text_script(
                sandbox,
                path=path,
                content=content,
                runtime_identity=identity,
                owner_user=identity["linux_user"],
                mode=0o600,
                error_code="AGENT_RUNTIME_ERROR",
                error_message=f"failed to install the dsh-superagent {what}",
            )

        await install(
            posixpath.join(config_dir, _TARGET_FILENAME),
            json.dumps(context.runtime_state_store, ensure_ascii=False, sort_keys=True),
            "runtime-state target",
        )
        await install(env_path, env_content, "profile environment")
        targets = await assistant_a2a_targets(
            assistant_id=str(getattr(context.template, "assistant_id", "") or ""),
            binding_id=context.platform_mcp_deployment_id,
            proxy_base_url=str(getattr(context.deployment_settings, "mcp_proxy_base_url", "") or ""),
        )
        await install(
            posixpath.join(config_dir, _AGENTS_FILENAME),
            json.dumps(targets, ensure_ascii=False, sort_keys=True),
            "A2A Agent list",
        )
        await self._install_profile_packages(commands, identity, packages)
        if token:
            await self._sign_in_github(commands, identity, token, install)
        sandbox_id = str(context.sandbox_id or "").strip()
        published = await commands.run(
            "python3 " + _STATE_SCRIPT + " gate publish --profile-env "
            + shlex.quote(env_path) + " --sandbox-id " + shlex.quote(sandbox_id),
            timeout=60,
        )
        if published.exit_code != 0 or "DSH_SUPERAGENT_PROFILE_INITIALIZED" not in published.output:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="failed to publish this box's dsh-superagent initialization: "
                + published.output.strip()[-500:],
                status_code=502,
            )
        fresh = "fresh=1" in published.output
        try:
            if not fresh and changed:
                # The model route and credentials are read when the harness
                # boots, so a changed profile takes effect by a restart.
                await restart_resident_program(
                    commands,
                    program=_HARNESS_PROGRAM,
                    lock=_SUPERVISE_LOCK,
                    marker=_PAUSING_MARKER,
                    label="dsh-superagent harness",
                    error_code="AGENT_RUNTIME_ERROR",
                )
            await install(digest_path, digest, "process fingerprint")
            await wait_until_answering(
                commands,
                "runuser -u " + shlex.quote(identity["linux_user"]) + " -- env HOME="
                + shlex.quote(identity["home_dir"]) + " " + _PROBE,
                _ANSWER_BUDGET_SECONDS,
                # The hop answers 502 until the harness behind it listens.
                waiting_statuses=("502", "503", "504"),
                foreign=_probe_foreign,
                unready=_probe_unready,
            )
            if fresh:
                await self._save(commands, identity)
        except BaseException as exc:
            if context.workspace_plan.operation == "runtime_start":
                try:
                    await self.quiesce_and_save_runtime_state(sandbox, runtime_identity=identity)
                except BaseException as save_error:
                    raise RuntimeStateSaveRequired(sandbox_id, identity) from save_error
            raise exc
        client = DshSuperagentRuntimeClient()
        manifest = await initialize_runtime_client(
            client, engine_kind=ENGINE_KIND, resume_session_key=None
        )
        return SessionRuntime(
            session_id=context.session_id,
            agent=None,
            sandbox=sandbox,
            sandbox_id=sandbox_id,
            engine_session_key=None,
            terminal_cwd=context.cwd,
            permission_mode=None,
            engine_kind=ENGINE_KIND,
            engine_client=client,
            engine_manifest=manifest,
            runtime_identity=identity,
            prepare_engine_input=context.prepare_engine_input,
        )

    async def _install_profile_packages(
        self, commands: Any, identity: dict[str, Any], packages: dict[str, Any]
    ) -> None:
        """Install the owner's plugin Bundles into the web profile as the Assistant's account."""

        script = _PROFILE_PACKAGES.replace("@STEP@", profile_package_step(packages))
        installed = await commands.run(
            "runuser -u " + shlex.quote(identity["linux_user"]) + " -- env HOME="
            + shlex.quote(identity["home_dir"]) + " DSH_HOME=" + shlex.quote(identity["config_dir"])
            + " bash -c " + shlex.quote(script),
            timeout=_INSTALL_BUDGET_SECONDS,
        )
        if installed.exit_code != 0 or "DSH_SUPERAGENT_PACKAGES_READY" not in installed.output:
            raise APIError(
                code="CONVERSATION_BOOTSTRAP_FAILED",
                message="installing this Assistant's plugin packages (Profile package.json) failed: "
                + " ".join(installed.output.split())[-500:],
                status_code=502,
            )

    async def _sign_in_github(self, commands: Any, identity: dict[str, Any], token: str, install: Any) -> None:
        """Sign the account's agent CLI in to GitHub with the owner's or the deployment's token."""

        # The account's own config directories: the token file is removed by the
        # account once its CLI is signed in, and the CLI writes beside it.
        prepared = await commands.run(
            "runuser -u " + shlex.quote(identity["linux_user"]) + " -- mkdir -p -m 0700 "
            + shlex.quote(posixpath.join(identity["home_dir"], ".config/astrabox"))
            + " " + shlex.quote(posixpath.join(identity["home_dir"], ".config/gh-agent")),
            timeout=30,
        )
        if prepared.exit_code != 0:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="failed to prepare the dsh-superagent account's config directories: "
                + prepared.output.strip()[-300:],
                status_code=502,
            )
        await install(
            posixpath.join(identity["home_dir"], ".config/astrabox/github-token"),
            token + "\n",
            "GitHub agent token",
        )
        login = await commands.run(
            "runuser -u " + shlex.quote(identity["linux_user"]) + " -- env HOME="
            + shlex.quote(identity["home_dir"]) + " " + _GH_LOGIN,
            timeout=120,
        )
        if login.exit_code != 0 or "ASTRABOX_GITHUB_AGENT_SIGNED_IN" not in login.output:
            # GitHub's own refusal, in the CLI's words, is what the owner can act on.
            refusal = " ".join(login.output.split())[-300:]
            raise owner_secret_action(
                "owner_secret_invalid",
                f"GitHub refused the agent account token ({refusal}). Replace it in this "
                "Assistant's settings.",
                keys=[GITHUB_TOKEN],
            )

    async def _save(self, commands: Any, identity: dict[str, Any]) -> None:
        env_path = posixpath.join(identity["config_dir"], _ENV_FILENAME)
        await confirm_forced_save(
            commands,
            "runuser -u " + shlex.quote(identity["linux_user"]) + " -- env HOME="
            + shlex.quote(identity["home_dir"]) + " python3 " + _STATE_SCRIPT
            + " mirror save --profile-env " + shlex.quote(env_path),
            _SAVE_MARKER,
        )

    async def quiesce_and_save_runtime_state(
        self, sandbox: Any, *, runtime_identity: dict[str, Any]
    ) -> None:
        """Stop the harness and the mirror, then save both archives and confirm it.

        A box whose state protocol never started (no gate, database, receipt or
        pending recovery) has nothing to save. Otherwise the image's restore runs
        first, so a fenced initial recovery a previous attempt left pending is
        published before the save reads the receipt.
        """

        identity = normalize_runtime_identity(runtime_identity)
        if not identity:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="dsh-superagent state save needs the Assistant runtime identity",
                status_code=500,
            )
        commands = sandbox.commands
        config_dir = identity["config_dir"]
        phase = "dsh_superagent_close_restarts"
        try:
            probe = (
                "from pathlib import Path; import sys\n"
                "present=False\n"
                "for p in sys.argv[1:]:\n"
                " try: Path(p).lstat(); present=True\n"
                " except FileNotFoundError: pass\n"
                "print('DSH_SUPERAGENT_STATE_PRESENT' if present else 'DSH_SUPERAGENT_STATE_NOT_STARTED')"
            )
            observed = await commands.run(shlex.join([
                "python3", "-c", probe, "/run/astrabox-dsh-superagent/profile-ready.json",
                posixpath.join(config_dir, "state/superagent/orchestrator.sqlite"),
                posixpath.join(config_dir, "astrabox-runtime-state-restored.json"),
                posixpath.join(config_dir, "astrabox-initial-state-recovery.json"),
            ]))
            if observed.exit_code != 0:
                raise RuntimeError("native state initialization could not be observed")
            if observed.output.strip() == "DSH_SUPERAGENT_STATE_NOT_STARTED":
                return
            await close_supervised_restarts(commands, lock=_SUPERVISE_LOCK, marker=_PAUSING_MARKER)
            for program, phase in (
                (_STATE_PROGRAM, "dsh_superagent_stop_state_mirror"),
                # The harness and the bundle's Sessions write the database.
                (_HARNESS_PROGRAM, "dsh_superagent_stop_harness"),
            ):
                await stop_supervised_program(commands, program)
            phase = "dsh_superagent_native_state_save"
            restored = await commands.run(
                self._as_account(identity, "python3 " + _STATE_SCRIPT + " gate restore --profile-env "
                                 + shlex.quote(posixpath.join(config_dir, _ENV_FILENAME))),
            )
            if restored.exit_code != 0:
                raise RuntimeError("dsh-superagent state restore before save failed: "
                                   + restored.output.strip()[-300:])
            await self._save(commands, identity)
        except Exception as exc:
            raise APIError(
                code="ASSISTANT_WORKSPACE_CONVERGENCE_FAILED",
                message=f"dsh-superagent runtime state barrier failed during {phase}",
                status_code=503,
                data={"phase": phase},
            ) from exc

    @staticmethod
    def _as_account(identity: dict[str, Any], command: str) -> str:
        return (
            "runuser -u " + shlex.quote(identity["linux_user"]) + " -- env HOME="
            + shlex.quote(identity["home_dir"]) + " " + command
        )

    async def panel_launch_path(self, sandbox: Any, *, runtime_identity: dict[str, Any]) -> str:
        """The harness's own launch URL, as a path on the panel's address."""

        identity = normalize_runtime_identity(runtime_identity)
        if not identity:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="dsh-superagent panel needs the Assistant runtime identity",
                status_code=500,
            )
        launch = posixpath.join(identity["home_dir"], ".deepseek-harness/web-url")
        result = await sandbox.commands.run("cat " + shlex.quote(launch), timeout=30)
        match = re.fullmatch(r"http://127\.0\.0\.1:\d+/\?(token=[A-Za-z0-9_-]+)", result.output.strip())
        if result.exit_code != 0 or match is None:
            raise APIError(
                code="ASSISTANT_WORKSPACE_NOT_READY",
                message="the dsh-superagent harness has not announced its login yet",
                status_code=409,
                data={"state": "STARTING"},
            )
        return "/?" + match.group(1)


register_engine_adapter(ENGINE_KIND, DshSuperagentEngineAdapter())
