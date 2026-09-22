"""In-tree binding of the engine-adapter conformance suite.

Runs :class:`astrabox.testing.engine_conformance.EngineAdapterContractSuite`
over BOTH built-in adapters, so the contract the suite encodes (identity,
capability coherence, required surface) is proven by real implementations —
exactly like the collection contract suite's sqlite binding.
"""

from __future__ import annotations

from astrabox.core.service.orchestrator.engine.claude_code import (
    ClaudeCodeEngineAdapter,
)
from astrabox.core.service.orchestrator.engine.hermes import HermesEngineAdapter
from astrabox.testing.engine_conformance import (
    EngineAdapterContractSuite,
    EngineClientContractSuite,
)


class TestClaudeCodeAdapterContract(EngineAdapterContractSuite):
    def make_adapter(self):
        return ClaudeCodeEngineAdapter()


class TestHermesAdapterContract(EngineAdapterContractSuite):
    def make_adapter(self):
        return HermesEngineAdapter()


class TestDeepSeekHarnessAdapterContract(EngineAdapterContractSuite):
    def make_adapter(self):
        from astrabox.core.service.orchestrator.engine.deepseek_harness import (
            DeepSeekHarnessEngineAdapter,
        )

        return DeepSeekHarnessEngineAdapter()


class TestCodexAdapterContract(EngineAdapterContractSuite):
    def make_adapter(self):
        from astrabox.core.service.orchestrator.engine.codex import CodexEngineAdapter

        return CodexEngineAdapter()


# ── EngineClient bindings — both built-ins, shape-divergent by design ────────


class TestClaudeCodeEngineClientContract(EngineClientContractSuite):
    def client_type(self) -> type:
        from astrabox.core.service.orchestrator.engine.claude_code_client import (
            ClaudeCodeEngineClient,
        )

        return ClaudeCodeEngineClient


class TestHermesEngineClientContract(EngineClientContractSuite):
    def client_type(self) -> type:
        from astrabox.core.service.orchestrator.engine.hermes import (
            HermesEngineClient,
        )

        return HermesEngineClient


class TestDeepSeekHarnessEngineClientContract(EngineClientContractSuite):
    def client_type(self) -> type:
        from astrabox.core.service.orchestrator.engine.deepseek_harness_client import (
            DeepSeekHarnessEngineClient,
        )

        return DeepSeekHarnessEngineClient


class TestPiAdapterContract(EngineAdapterContractSuite):
    def make_adapter(self):
        from astrabox.core.service.orchestrator.engine.pi import PiEngineAdapter

        return PiEngineAdapter()


class TestPiEngineClientContract(EngineClientContractSuite):
    def client_type(self) -> type:
        from astrabox.core.service.orchestrator.engine.pi_client import PiEngineClient

        return PiEngineClient


class TestCodexEngineClientContract(EngineClientContractSuite):
    def client_type(self) -> type:
        from astrabox.core.service.orchestrator.engine.codex_client import (
            CodexEngineClient,
        )

        return CodexEngineClient


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
