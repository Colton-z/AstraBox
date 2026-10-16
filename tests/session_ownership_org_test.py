"""Ownership chokepoint + org-dimension stamping — the two multi-user invariants.

Pins the properties the multi-user seam stands on, so a refactor cannot quietly
regress them:

* **Ownership is a query-filter fact, never a caller check.**
  ``SessionRepository.get_owned_session`` folds the owner INTO the filter, so a
  wrong-owner lookup is atomically ``None`` (nothing for a caller to forget) and
  a deleted row is invisible. ``SessionService.must_get_owned_session`` maps that
  ``None`` to ``SESSION_NOT_FOUND`` 404 — a wrong owner is indistinguishable from
  a missing session (existence is not revealed).

* **Session and Vault create paths stamp the caller's org_id.** The turn and
  lifecycle workers rebuild the caller's ``UserContext`` from the Session row,
  so a Session that lost the org would run later commands under the default
  organization.

The PostgreSQL test database backs the ownership and vault-storage
assertions; the session stamp site is exercised through its real service method
with a payload-capturing repo, so the assertion is on the exact document the
create path assembles.
"""

from __future__ import annotations

import base64
import types
from typing import Any

import pytest

from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.vault_repository import VaultRepository
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext, default_org_id
from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.session_service import SessionService
from astrabox.core.service.orchestrator.vault_service import VaultService

# Deterministic 32-byte master key (urlsafe b64) so the local secret store needs
# no key file when a VaultService is constructed.
_MASTER_KEY = base64.urlsafe_b64encode(b"k" * 32).decode()

#: One of alice's sessions, as the session table requires it.
_SESSION = {"session_id": "s1", "user_id": "alice", "session_kind": "agent_chat", "state": "READY"}


@pytest.fixture(autouse=True)
def _clean_org_baseline(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A clean org baseline and a master key for the local secret store.

    ``ASTRABOX_DEFAULT_ORG`` is cleared so the default-org assertions are not
    perturbed by the ambient environment. ``monkeypatch`` restores the prior env
    after the test; the trailing ``cache_clear`` forces the next module's
    ``get_settings`` to re-read it.
    """
    monkeypatch.delenv("ASTRABOX_DEFAULT_ORG", raising=False)
    monkeypatch.setenv("ASTRABOX_VAULT_MASTER_KEY", _MASTER_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Fakes — the create paths touch a template + model resolver + repo; only the   #
# repo's captured payload is under test, so the rest are the smallest stand-ins #
# that let the real service method run.                                         #
# --------------------------------------------------------------------------- #
class _CapturingSessionsRepo:
    """Captures the payload ``create_session_record`` assembles (the stamp site)."""

    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    async def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payload = dict(payload)
        return dict(payload)


class _FakeTemplateService:
    def __init__(self, template: Any) -> None:
        self._template = template

    async def resolve_session_harness(self, session: dict[str, Any]) -> Any:
        return self._template

    async def resolve_agent_harness(
        self, agent_id: str, viewer_user_id: str | None = None
    ) -> Any:
        return self._template


class _FakeRuntimeManager:
    @staticmethod
    def resolve_template_model_name(template: Any) -> str | None:
        return "model-x"


def _session_service(sessions_repo: Any, template: Any | None = None) -> SessionService:
    """Build a SessionService wired to *sessions_repo*; unused deps are stand-ins.

    ``must_get_owned_session`` and ``create_session_record`` only reach the repo,
    template service, and model resolver — the remaining constructor deps are
    never touched on these paths.
    """
    return SessionService(
        sessions_repo=sessions_repo,
        agent_config=_FakeTemplateService(template),  # type: ignore[arg-type]
        runtime_manager=_FakeRuntimeManager(),  # type: ignore[arg-type]
        ttl_seconds=3600,
        spawn_background_task=lambda *a, **k: None,
    )


# ── (a) repository ownership scoping ────────────────────────────────────────
@pytest.mark.usefixtures("database")
async def test_get_owned_session_is_owner_scoped_and_hides_deleted() -> None:
    repo = SessionRepository()
    await repo.create_session(_SESSION)

    owned = await repo.get_owned_session("s1", "alice")
    assert owned is not None
    assert owned["session_id"] == "s1"

    # Wrong owner is atomically None — ownership is IN the filter.
    assert await repo.get_owned_session("s1", "bob") is None

    # A soft-deleted row is invisible to the owner too.
    assert await repo.soft_delete("s1", "alice") is True
    assert await repo.get_owned_session("s1", "alice") is None


# ── (b) service maps wrong owner → 404 ──────────────────────────────────────
@pytest.mark.usefixtures("database")
async def test_must_get_owned_session_maps_wrong_owner_to_not_found_404() -> None:
    repo = SessionRepository()
    await repo.create_session(_SESSION)
    service = _session_service(repo)

    got = await service.must_get_owned_session(UserContext(user_id="alice"), "s1")
    assert got["session_id"] == "s1"

    with pytest.raises(APIError) as ctx:
        await service.must_get_owned_session(UserContext(user_id="bob"), "s1")
    assert ctx.value.code == "SESSION_NOT_FOUND"
    assert ctx.value.status_code == 404


# ── (c) org stamping at resource create ─────────────────────────────────────
@pytest.mark.usefixtures("database")
async def test_vault_create_stamps_org_id_from_owner() -> None:
    repo = VaultRepository()
    service = VaultService(vault_repo=repo)

    acme = await service.create_vault(
        UserContext(user_id="u", org_id="acme"), display_name="A"
    )
    stored = await repo.get_vault(acme["vault_id"])
    assert stored is not None
    assert stored["org_id"] == "acme"

    # A default UserContext carries the deployment org.
    default = await service.create_vault(UserContext(user_id="u"), display_name="B")
    stored_default = await repo.get_vault(default["vault_id"])
    assert stored_default is not None
    assert stored_default["org_id"] == default_org_id() == "default"


async def test_session_create_payload_stamps_the_callers_org_id() -> None:
    # A startup acts for the caller it reads off the Session row, so the row
    # carries the caller's organization.
    template = types.SimpleNamespace(name="tmpl", sandbox_backend="open_sandbox")
    repo = _CapturingSessionsRepo()
    service = _session_service(repo, template=template)
    await service.create_session_record(
        UserContext(user_id="alice", org_id="acme"),
        workspace_ref={"kind": "agent", "agent_id": "a1"},
        agent_id="a1",
    )
    assert repo.payload is not None
    assert repo.payload["user_id"] == "alice"
    assert repo.payload["org_id"] == "acme"
