"""Engine-client registration refuses a client the platform cannot call."""

from __future__ import annotations


def test_a_client_that_drops_a_contract_keyword_is_refused() -> None:
    """Presence and kind are not enough to complete a turn.

    The platform calls this surface by keyword. A method that exists, is
    async, and simply lacks one of the contract's keyword-only parameters
    passes every other structural check and then raises "unexpected keyword
    argument" on the first real turn — after a box has been built and someone
    has typed something. This is the check that moves that failure to
    registration, and it is fed the exact shape that reached a testbed.
    """

    import pytest

    from astrabox.core.service.orchestrator.engine.base import (
        validate_engine_client_type,
    )
    from astrabox.core.service.orchestrator.engine.codex_client import (
        CodexEngineClient,
    )

    class DropsTheKeyword(CodexEngineClient):
        async def begin_delivery(self, command):  # type: ignore[override]
            raise NotImplementedError

    with pytest.raises(TypeError, match="does not accept consumption_confirmed"):
        validate_engine_client_type(DropsTheKeyword)

    class AbsorbsIt(CodexEngineClient):
        async def begin_delivery(self, command, **kwargs):  # type: ignore[override]
            raise NotImplementedError

    # Taking **kwargs is accepting them; the check must not punish that.
    validate_engine_client_type(AbsorbsIt)
