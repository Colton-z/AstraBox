"""Explicit identity register API — seam symmetry for vendored deployments.

Every other seam has a ``register_*()`` composition-root call; identity only
had entry-point discovery + the in-tree builtin bridge, so a deployment that
strips dist-info (vendored wheel) could not install a custom SSO resolver
without forking the loader. These tests pin the register path:

* an explicitly registered resolver wins over entry-point discovery and the
  builtin bridge;
* class targets are instantiated, instances returned as-is;
* blank names / None resolvers fail loud;
* unknown names still fail loud (registration does not soften the loader).
"""

from __future__ import annotations

import pytest

import astrabox.providers.identity as identity


@pytest.fixture(autouse=True)
def _clean_registries(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(identity, "_WEB_IDENTITY_RESOLVERS", {})
    yield


class _CorpSSO:
    @staticmethod
    def login_url(next_url: str = "/") -> None:
        _ = next_url
        return None

    async def resolve(self, headers):  # pragma: no cover - shape only
        return None


class _IncompleteSSO:
    async def resolve(self, headers):  # pragma: no cover - shape only
        return None


def test_registered_web_resolver_wins_for_its_name() -> None:
    instance = _CorpSSO()
    identity.register_web_identity_resolver("corp_sso", instance)
    assert identity.load_web_identity_resolver("corp_sso") is instance


def test_registered_class_target_is_instantiated() -> None:
    identity.register_web_identity_resolver("corp_sso", _CorpSSO)
    resolved = identity.load_web_identity_resolver("corp_sso")
    assert isinstance(resolved, _CorpSSO)


def test_registration_overrides_builtin_name() -> None:
    # Last registration wins even against an in-tree name — same semantics as
    # register_sandbox and friends.
    instance = _CorpSSO()
    identity.register_web_identity_resolver("jwt", instance)
    assert identity.load_web_identity_resolver("jwt") is instance


def test_blank_name_and_none_resolver_fail_loud() -> None:
    with pytest.raises(RuntimeError, match="non-empty"):
        identity.register_web_identity_resolver("  ", _CorpSSO())
    with pytest.raises(RuntimeError, match="must not be None"):
        identity.register_web_identity_resolver("x", None)
    with pytest.raises(RuntimeError, match=r"login_url\(\)"):
        identity.register_web_identity_resolver("incomplete", _IncompleteSSO())


def test_unknown_name_still_fails_loud() -> None:
    identity.register_web_identity_resolver("corp_sso", _CorpSSO())
    with pytest.raises(RuntimeError, match="no web identity resolver"):
        identity.load_web_identity_resolver("not-registered-anywhere")
