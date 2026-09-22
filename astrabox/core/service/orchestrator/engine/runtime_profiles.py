"""The platform's runtime-identity rules, composed per tenancy.

Sandbox tenancy is a fact about boxes: how many conversations one box
carries, backed by OpenSandbox isolated sessions — a substrate capability
reachable only through the provider seam. So everything that varies with it
(account naming, home placement, the account-assembly commands) is composed
here, once, for every engine; an adapter contributes only its own facts
(:class:`EngineWorkloadDeclaration`) and never repeats a platform rule.
"""

from __future__ import annotations

from astrabox.core.service.orchestrator.engine.capabilities import (
    EngineRuntimeProfileDeclaration,
    EngineWorkloadDeclaration,
    capabilities_for_engine_kind,
)
from astrabox.seams.sandbox import (
    SANDBOX_TENANCIES,
    SANDBOX_TENANCY_AGENT,
)

# The Claude image creates this account before the resident control server can
# accept a Session.  That is what makes the declaration usable by a prewarm
# Pool: the box exists before any Session has supplied an identity, so a
# per-conversation account cannot be assembled at claim time on this tenancy.
SANDBOX_IMAGE_WORKLOAD_USER = "agent"
SANDBOX_IMAGE_WORKLOAD_HOME = f"/home/{SANDBOX_IMAGE_WORKLOAD_USER}"

#: Where a conversation's own engine service listens under the shared-box
#: tenancy. A placement rule like the account naming above: the uid is unique
#: per conversation and monotonic, so the port derives from it instead of
#: being tracked by a second allocator. The span exceeds any box's
#: conversation capacity; a collision is caught by the second service failing
#: to bind — a refusal, never a crossed connection.
RUNNER_PORT_BASE = 9000
RUNNER_PORT_SPAN = 500


def runner_port_for_uid(uid: int) -> int:
    """The port this conversation's engine service listens on."""

    return RUNNER_PORT_BASE + (int(uid) % RUNNER_PORT_SPAN)


def runner_ports_for_uid(uid: int) -> tuple[int, ...]:
    """Every port in the box this conversation's uid spoken for.

    The outward one above, and the in-box loopback upstream a span higher:
    an engine that fronts its own service (dsh's forwarder ahead of the
    harness web server) derives the second from the first by this same rule,
    so a conversation reserves the pair. Checking only the outward port would
    miss a collision on the upstream port and prevent the service from starting.
    """

    outward = runner_port_for_uid(uid)
    return (outward, outward + RUNNER_PORT_SPAN)
SANDBOX_IMAGE_WORKSPACE_DIR = "/workspace"

#: The shared tenancy names accounts by the product the session belongs to.
#: Session kinds are platform vocabulary, so the prefix is a platform rule:
#: an Agent conversation is ``conv_``, an Assistant conversation ``asst_``.
_SHARED_USERNAME_PREFIX = {
    "agent_chat": "conv_",
    "assistant_chat": "asst_",
}

#: What the platform's own account assembly runs on the shared tenancy
#: (``conversation_identity``'s provisioning script: groupadd, then useradd).
#: Appended to the engine's commands so the warmup probe demands them of any
#: image asked to carry more than one conversation.
_SHARED_ACCOUNT_COMMANDS = ("useradd", "groupadd")


def composed_runtime_profile(
    engine_kind: str,
    sandbox_tenancy: str,
    *,
    session_kind: str = "agent_chat",
) -> EngineRuntimeProfileDeclaration:
    """Join the platform's tenancy rules with one engine's declared facts.

    Total over ``SANDBOX_TENANCIES`` × known session kinds, deliberately:
    whether a BOX can carry the shared tenancy is the box's own answer
    (execd's ``isolation.capabilities()`` probe at claim time), and whether an
    engine's INTEGRATION honours it is ``conversation_placement`` — both
    enforced where they are known, neither by refusing to compose a shape.
    """

    tenancy = str(sandbox_tenancy or "").strip()
    if tenancy not in SANDBOX_TENANCIES:
        raise ValueError(
            f"unknown sandbox tenancy {tenancy!r}; expected one of {SANDBOX_TENANCIES}"
        )
    prefix = _SHARED_USERNAME_PREFIX.get(str(session_kind or "").strip())
    if prefix is None:
        raise ValueError(
            f"no shared-account prefix for session_kind={session_kind!r}; "
            f"known: {sorted(_SHARED_USERNAME_PREFIX)}"
        )
    workload = capabilities_for_engine_kind(engine_kind).workload
    return _compose(workload, tenancy=tenancy, prefix=prefix)


def _compose(
    workload: EngineWorkloadDeclaration,
    *,
    tenancy: str,
    prefix: str,
) -> EngineRuntimeProfileDeclaration:
    shared = tenancy == SANDBOX_TENANCY_AGENT
    commands = tuple(workload.required_commands)
    if shared:
        commands = commands + tuple(
            command
            for command in _SHARED_ACCOUNT_COMMANDS
            if command not in commands
        )
    return EngineRuntimeProfileDeclaration(
        sandbox_tenancy=tenancy,
        username_template=(
            prefix + "{session_hash}" if shared else SANDBOX_IMAGE_WORKLOAD_USER
        ),
        home_template=(
            "/home/conversations/{username}"
            if shared
            else SANDBOX_IMAGE_WORKLOAD_HOME
        ),
        workspace_template=SANDBOX_IMAGE_WORKSPACE_DIR,
        workspace_source_template=(
            "{home}/workspace" if shared else SANDBOX_IMAGE_WORKSPACE_DIR
        ),
        config_dir_name=workload.config_dir_name,
        config_env_var=workload.config_env_var,
        required_commands=commands,
    )
