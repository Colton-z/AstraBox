"""Every (engine, tenancy) pair composes the profile that engine requires.

Sandbox tenancy is composed by the platform, in
runtime_profiles.composed_runtime_profile, rather than declared by each
engine. These tests pin which tenancies resolve, which are refused and why,
and the shared-tenancy identity every engine inherits from the composition.
"""

from __future__ import annotations

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.providers import register_builtin_providers
from astrabox.core.service.orchestrator.engine.runtime_profiles import (
    composed_runtime_profile,
)
from astrabox.core.service.orchestrator.runtime.runtime_profile import (
    resolve_runtime_profile,
)


# The resolver reads placements from the engine registry, which fills as a
# side effect of importing the adapter modules; running this file alone must
# not depend on a neighbouring test having imported them first.
register_builtin_providers()


class _Template:
    def __init__(self, engine_kind: str, sandbox_tenancy: str | None) -> None:
        self.engine_kind = engine_kind
        self.sandbox_tenancy = sandbox_tenancy


def test_shared_tenancy_is_refused_for_box_account_integrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal names a pending refactor, never an engine capability.

    No shipped engine declares box_account any more — every one was
    instantiated per conversation — so the gate is pinned through a stubbed
    declaration: the NEXT box-service integration arrives with this
    placement, and the error must say the integration is pending rather
    than "the engine cannot", which would be false and steer an operator
    away from a mode the engine serves fine once the integration catches
    up.
    """

    from types import SimpleNamespace

    from astrabox.core.service.orchestrator.engine import capabilities

    # The resolver imports the lookup at call time, so patching the source
    # module is what its next call observes.
    monkeypatch.setattr(
        capabilities,
        "capabilities_for_engine_kind",
        lambda kind: SimpleNamespace(conversation_placement="box_account"),
    )
    with pytest.raises(APIError) as caught:
        resolve_runtime_profile(_Template("claude_code", "agent"))
    assert caught.value.code == "UNSUPPORTED_SANDBOX_TENANCY"
    assert "not instantiated per conversation" in caught.value.message
    assert "pending" in caught.value.message
    assert (caught.value.data or {}).get("conversation_placement") == "box_account"


@pytest.mark.parametrize("engine_kind", ["codex", "deepseek_harness", "pi"])
def test_the_composition_itself_serves_every_engine_both_tenancies(
    engine_kind: str,
) -> None:
    """No engine is locked out of a tenancy by a missing declaration.

    The refusal above is the integration gate; the composed shape beneath it
    exists for every engine so the phase-2 instantiation work changes ONE
    field (conversation_placement) and inherits a correct identity, rather
    than hand-writing the platform's account rules a fourth time.
    """

    p = composed_runtime_profile(engine_kind, "agent")
    assert p.username_template == "conv_{session_hash}"
    assert p.home_template == "/home/conversations/{username}"
    assert p.required_commands[-2:] == ("useradd", "groupadd")


def test_an_unknown_tenancy_still_fails_loud() -> None:
    with pytest.raises(APIError) as caught:
        resolve_runtime_profile(_Template("claude_code", "flat"))
    assert caught.value.code == "UNSUPPORTED_SANDBOX_TENANCY"


def test_an_unset_tenancy_still_defaults_to_conversation() -> None:
    p = resolve_runtime_profile(_Template("claude_code", None))
    assert p.sandbox_tenancy == "conversation"
    assert p.username_template == "agent"


@pytest.mark.parametrize("engine_kind", ["deepseek_harness", "codex", "pi"])
def test_instantiated_box_service_engines_resolve_the_shared_tenancy(
    engine_kind: str,
) -> None:
    """Box-service engines instantiated per conversation take the tenancy.

    Their placement is per_conversation_account with each image's
    serve-conversation stack behind it, which is exactly what shared tenancy
    needs, so an agent-tenancy environment resolves a composed shared profile
    rather than being refused.
    """

    p = resolve_runtime_profile(_Template(engine_kind, "agent"))
    assert p.username_template == "conv_{session_hash}"
    assert p.home_template == "/home/conversations/{username}"
    assert p.required_commands[-2:] == ("useradd", "groupadd")
@pytest.mark.parametrize("engine_kind", ["deepseek_harness", "codex", "pi"])
def test_the_shared_service_trigger_carries_the_conversation_home(
    engine_kind: str,
) -> None:
    """The launch line exports HOME for the conversation account.

    The isolated session's shell does not set the account's HOME, and every
    serve-conversation script derives its per-conversation paths from it; a
    trigger without it runs against the shell's own HOME and never binds the
    outward port.
    """

    from astrabox.core.service.orchestrator.engine.registry import (
        get_engine_adapter,
    )

    line = get_engine_adapter(engine_kind).shared_conversation_service_launch(
        home="/home/conversations/conv_abc",
        workspace="/home/conversations/conv_abc/workspace",
        port=9042,
    )
    assert "HOME=/home/conversations/conv_abc " in line
    assert "TMPDIR=/home/conversations/conv_abc/.astrabox-spool " in line
    assert line.startswith(
        "mkdir -p /home/conversations/conv_abc/.astrabox-spool && "
    )
    assert "/workspace/.astrabox-spool" not in line
    assert " 9042 " in line
    # All three descriptors must be taken over: execd's foreground-run
    # completion scan reads the session's shared stdout, and a service that
    # inherits the session's descriptors corrupts the next run's end-marker.
    assert "</dev/null" in line
