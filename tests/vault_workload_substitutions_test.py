"""A writer must not strip a sibling workload's identity from the gateway.

The 2026-09-02 incident, as a test. One box's model gateway binding is replaced
WHOLE on every write and its substitution list is the one part of a vault the
read API never returns, so a writer composing only its own identity dropped
every sibling's — silently, because nothing can read back what was lost. The
conversation that owned the dropped one sent its placeholder to the gateway
verbatim and spent 174 seconds on `401 Virtual Key expected`.

Nothing in the suite would have failed. These would.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrabox.providers.open_sandbox.credential_vault import (
    build_vault_write,
    carry_workload_substitutions,
    workload_of_credential_name,
    workload_substitutions,
)
from astrabox.providers.open_sandbox.executor import apply_open_sandbox_vault
from astrabox.seams.egress_credentials import (
    workload_credential_name,
    workload_model_placeholder,
)

GATEWAY = "https://gateway.example.com"


class FakeVault:
    """The parts of the sidecar's vault this path uses, with real semantics.

    `get` returns SANITIZED state: names and revisions, and no substitutions —
    which is the property the whole defect turns on, so the fake refuses to
    return them rather than making the test easier than reality.
    """

    def __init__(self, credentials: list[str]) -> None:
        self.credentials = [
            SimpleNamespace(name=name, source_type="inline", revision=1)
            for name in credentials
        ]
        self.bindings: list[SimpleNamespace] = [
            SimpleNamespace(name="astrabox-model-gateway", revision=1, match=None, auth=None)
        ]
        self.revision = 1
        self.written: list[object] = []

    async def create(self, **_kwargs: object) -> None:
        raise RuntimeError("HTTP 409 vault already exists")

    async def get(self) -> SimpleNamespace:
        return SimpleNamespace(
            credentials=list(self.credentials),
            bindings=list(self.bindings),
            revision=self.revision,
        )

    async def patch(
        self,
        *,
        expected_revision: int,
        credentials: dict | None = None,
        bindings: dict | None = None,
    ) -> None:
        assert expected_revision == self.revision, "a blind write must not be possible"
        self.revision += 1
        for entry in (bindings or {}).get("replace", []) or []:
            self.written.append(entry)
        for entry in (bindings or {}).get("add", []) or []:
            self.written.append(entry)


def handle_for(vault: FakeVault) -> SimpleNamespace:
    return SimpleNamespace(
        sandbox_id="box-1",
        sidecar_faces=SimpleNamespace(credential_vault=vault),
    )


def gateway_write() -> tuple[list, list]:
    """What an ordinary session start composes: the box identity, nothing else."""
    return build_vault_write(
        credential="sk-real-gateway-key",
        credential_header="authorization",
        base_url=GATEWAY,
        request_paths=("/v1/*",),
    )


def placeholders_of(binding: object) -> set[str]:
    auth = getattr(binding, "auth", None)
    return {
        str(getattr(item, "placeholder", ""))
        for item in (getattr(auth, "substitutions", None) or [])
    }


@pytest.mark.asyncio
async def test_a_writer_that_never_heard_of_a_workload_keeps_its_substitution() -> None:
    """The incident itself: a cold session start, on a box holding a slot.

    The writer composes the box identity and knows nothing about the slot that
    was prepared four seconds earlier. Before the substitutions were derived
    from the vault, this write is what removed the slot's line.
    """
    live = workload_credential_name("slot-12b7f006")
    vault = FakeVault(["astrabox-model-gateway", live])

    await apply_open_sandbox_vault(
        handle_for(vault), vault_write=gateway_write(), session_id="session-cold"
    )

    assert vault.written, "the gateway binding must have been written"
    assert workload_model_placeholder("slot-12b7f006") in placeholders_of(
        vault.written[-1]
    ), "a sibling workload's bearer was dropped and cannot be read back to notice"


@pytest.mark.asyncio
async def test_two_workloads_on_one_box_both_survive_a_third_writer() -> None:
    """Not one sibling — every one of them, however many share the box."""
    names = [workload_credential_name(f"slot-{n}") for n in ("aaa", "bbb", "ccc")]
    vault = FakeVault(["astrabox-model-gateway", *names])

    await apply_open_sandbox_vault(
        handle_for(vault), vault_write=gateway_write(), session_id="session-cold"
    )

    written = placeholders_of(vault.written[-1])
    for suffix in ("aaa", "bbb", "ccc"):
        assert workload_model_placeholder(f"slot-{suffix}") in written


@pytest.mark.asyncio
async def test_the_workload_this_write_adds_is_carried_too() -> None:
    """A prepare writes its credential and its substitution in one patch.

    Deriving only from what the READ returned would leave the new workload's
    own line out — the box would hold a credential nothing routes to, and the
    slot's first call would be refused exactly as a stripped one is.
    """
    vault = FakeVault(["astrabox-model-gateway"])
    credentials, bindings = gateway_write()
    from opensandbox.models.sandboxes import Credential, InlineCredentialSource

    credentials = [
        *credentials,
        Credential(
            name=workload_credential_name("slot-new"),
            source=InlineCredentialSource(type="inline", value="sk-slot-key"),
        ),
    ]

    await apply_open_sandbox_vault(
        handle_for(vault), vault_write=(credentials, bindings), session_id="prepare"
    )

    assert workload_model_placeholder("slot-new") in placeholders_of(vault.written[-1])


@pytest.mark.asyncio
async def test_without_the_derivation_the_sibling_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the same write, with the derivation taken back out.

    An assertion that passes both with and without the mechanism it is meant
    to prove tests nothing. Neutering `carry_workload_substitutions` to the
    identity restores exactly the composition the five writers used on
    2026-09-02, and the sibling's bearer must then be gone — which is what
    makes the tests above evidence rather than decoration.
    """
    from astrabox.providers.open_sandbox import executor

    monkeypatch.setattr(
        executor, "carry_workload_substitutions", lambda bindings, _credentials: bindings
    )
    live = workload_credential_name("slot-12b7f006")
    vault = FakeVault(["astrabox-model-gateway", live])

    await apply_open_sandbox_vault(
        handle_for(vault), vault_write=gateway_write(), session_id="session-cold"
    )

    assert workload_model_placeholder("slot-12b7f006") not in placeholders_of(
        vault.written[-1]
    )


def test_only_the_model_gateway_binding_carries_workload_bearers() -> None:
    """An MCP binding must not splice a credential into a matching string."""
    _credentials, mcp_bindings = build_vault_write(
        credential="sk-mcp-gateway",
        credential_header="authorization",
        base_url=GATEWAY,
        request_paths=("/bright_data/mcp",),
        name="astrabox-mcp-gateway-litellm",
    )
    completed = carry_workload_substitutions(
        mcp_bindings,
        [SimpleNamespace(name=workload_credential_name("slot-aaa"))],
    )
    assert workload_model_placeholder("slot-aaa") not in placeholders_of(completed[0])


def test_a_credential_name_and_a_placeholder_are_one_workloads_two_derivations() -> None:
    """Why no shadow of the write-only state is needed at all."""
    assert workload_of_credential_name(workload_credential_name("slot-x")) == "slot-x"
    assert workload_of_credential_name("astrabox-model-gateway") is None
    assert workload_of_credential_name("astrabox-mcp-gateway-litellm") is None
    assert workload_substitutions(
        [
            SimpleNamespace(name="astrabox-model-gateway"),
            SimpleNamespace(name=workload_credential_name("slot-y")),
        ]
    ) == [(workload_model_placeholder("slot-y"), workload_credential_name("slot-y"))]
