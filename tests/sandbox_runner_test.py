"""The in-box runner's permission gate fails closed: an error inside it denies."""

from __future__ import annotations

from astrabox.core.service.orchestrator.sandbox_runner import (
    InteractionBroker,
)


# --- event pump -------------------------------------------------------------


async def test_a_gate_that_errors_denies_instead_of_leaking() -> None:
    """Fail CLOSED: an exception inside the gate must become a deny decision.

    Under the CLI's bypass base mode an errored hook falls through to "run
    it", so an exception escaping the gate executes an unapproved tool. The
    belt converts any unexpected error into the one answer that cannot be
    wrong.
    """

    class _ExplodingSender:
        host_connected = True

        async def send(self, *args, **kwargs):
            raise RuntimeError("wire fell over")

    broker = InteractionBroker(_ExplodingSender(), wait_budget_s=5.0)  # type: ignore[arg-type]
    out = await broker.pre_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": "x"}}, "t1", None
    )

    hook = out["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    assert "permission gate error" in hook["permissionDecisionReason"]
