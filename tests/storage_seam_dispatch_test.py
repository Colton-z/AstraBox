"""What selects a storage provider, and what happens when nothing does.

Dispatch reads the deployment's configured provider name, and deliberately not
the sandbox backend a session persisted. Those are two axes that move
separately — which runtime creates a box, and where the files it works on
live — and keying storage on the backend has a consequence sharper than the
argument for it: a provider is then reachable only by a deployment running a
same-named backend.

These pin the behaviour that makes the key safe: an unknown name is refused
rather than resolved to something, and the refusal lands at boot rather than at
the first workspace a user creates.

Design: `docs/maintainers/workspace-storage-seam.md` §3.
"""

from __future__ import annotations

import pytest

import astrabox.seams.storage as storage_seam
from astrabox.bootstrap import BootstrapConfigError, _assert_storage_provider_is_registered
from astrabox.providers.storage.mounted_volume import MountedVolumeStorage
from astrabox.seams.storage import (
    StorageProvider,
    WorkspaceRef,
    register_storage,
    set_configured_storage_provider,
    storage_provider,
)


class _Elsewhere(StorageProvider):
    """A second registered provider, so "exactly one" stops being true."""

    name = "elsewhere"

    async def prepare(
        self,
        ref: WorkspaceRef,
        *,
        box: object,
        box_path: str,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        return None


@pytest.fixture(autouse=True)
def _isolated_storage_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_seam, "_PROVIDERS", dict(storage_seam._PROVIDERS))
    monkeypatch.setattr(storage_seam, "_CONFIGURED", storage_seam._CONFIGURED)


def test_the_configured_name_selects_the_provider() -> None:
    register_storage(_Elsewhere.name, _Elsewhere())
    set_configured_storage_provider("elsewhere")

    assert storage_provider().name == "elsewhere"
    # And an explicit name still wins over the configured one, which is what
    # lets a caller address a specific medium without reconfiguring the process.
    assert storage_provider("mounted_volume").name == "mounted_volume"


def test_no_provider_is_named_after_a_sandbox_backend() -> None:
    """Storage provider names stay disjoint from sandbox backend names.

    A collision would let storage dispatch resolve a sandbox backend identifier
    instead of the configured storage medium.
    """
    from astrabox.providers import register_builtin_providers
    from astrabox.seams.sandbox import registered_sandbox_names

    register_builtin_providers()
    assert "mounted_volume" in storage_seam._PROVIDERS
    assert set(storage_seam._PROVIDERS) & set(registered_sandbox_names()) == set()


def test_an_unknown_name_is_refused_and_lists_what_is_registered() -> None:
    """A typo must not resolve to a medium that never held the files.

    Naming the registered set is the difference between an error an operator can
    act on and one that sends them to the source.
    """
    set_configured_storage_provider("s3")

    with pytest.raises(RuntimeError) as caught:
        storage_provider()
    assert "'s3'" in str(caught.value)
    assert "mounted_volume" in str(caught.value)


def test_one_registered_provider_is_the_unambiguous_answer() -> None:
    """Matches `default_sandbox_backend`: with one provider there is no second
    answer to choose wrongly between."""
    storage_seam._PROVIDERS.clear()
    register_storage(MountedVolumeStorage.name, MountedVolumeStorage())
    set_configured_storage_provider("")

    assert storage_provider().name == "mounted_volume"


def test_two_registered_and_none_configured_still_fails() -> None:
    """The half of the previous test that keeps it from being a fallback."""
    register_storage(_Elsewhere.name, _Elsewhere())
    set_configured_storage_provider("")

    with pytest.raises(RuntimeError) as caught:
        storage_provider()
    assert "ASTRABOX_STORAGE_PROVIDER" in str(caught.value)


def test_boot_refuses_a_storage_provider_nothing_registered() -> None:
    """Where the refusal lands matters as much as that it exists.

    Resolution is otherwise first reached inside a user's request, long after
    the deployment has been up and looking healthy.
    """
    set_configured_storage_provider("s3")

    with pytest.raises(BootstrapConfigError) as caught:
        _assert_storage_provider_is_registered()
    assert "ASTRABOX_STORAGE_PROVIDER" in str(caught.value)


def test_boot_passes_on_the_default() -> None:
    set_configured_storage_provider("mounted_volume")
    _assert_storage_provider_is_registered()


def test_the_composition_root_actually_runs_that_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate that exists and a gate that runs are two facts.

    The test above proves the check refuses; this proves `bootstrap()` performs
    it. Without this pair, deleting one line from the composition root leaves
    every other test in this file green.
    """
    import astrabox.bootstrap as bootstrap_module

    monkeypatch.setenv("ASTRABOX_STORAGE_PROVIDER", "s3")
    from astrabox.config.settings import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(BootstrapConfigError) as caught:
            bootstrap_module.bootstrap()
        assert "ASTRABOX_STORAGE_PROVIDER" in str(caught.value)
    finally:
        get_settings.cache_clear()


def test_every_builtin_storage_provider_is_in_the_entry_point_table() -> None:
    """Registration in-tree and discovery from an installed distribution.

    `mounted_volume` shipped registered as a builtin and absent from
    `[project.entry-points."astrabox.providers.storage"]`, so a checkout
    resolved it and an installed wheel did not.

    The builtin set is read off the package's own modules rather than off the
    live registry, which any test that registered a double has already added
    to, and rather than off a list here, which would go stale the same way the
    entry-point table did.
    """

    import importlib
    import pkgutil
    import tomllib
    from pathlib import Path

    import astrabox.providers.storage as storage_package
    from astrabox.seams.storage import ENTRY_POINT_GROUP, StorageProvider

    builtin: set[str] = set()
    for module in pkgutil.iter_modules(storage_package.__path__):
        loaded = importlib.import_module(f"{storage_package.__name__}.{module.name}")
        for attribute in vars(loaded).values():
            if (
                isinstance(attribute, type)
                and issubclass(attribute, StorageProvider)
                and attribute is not StorageProvider
                and attribute.__module__ == loaded.__name__
            ):
                builtin.add(attribute.name)
    assert builtin, "found no bundled provider; this test would pass vacuously"

    root = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(manifest["project"]["entry-points"][ENTRY_POINT_GROUP])

    assert builtin <= declared, (
        f"registered in-tree but not discoverable from an installed "
        f"distribution: {sorted(builtin - declared)}"
    )
