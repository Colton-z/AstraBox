"""The credential-seam gate must catch the import that caused the incident.

A checker written after the fact proves nothing by being green: the tree was
green on every other gate the day a leaked placeholder reached the model
gateway verbatim. So these feed it that exact shape and require it to fire,
and feed it the legitimate shapes and require it to stay quiet.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "scripts/check_credential_seam.py"


def load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_credential_seam", CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A miniature astrabox/ the checker can be pointed at."""
    checker = load_checker()
    package = tmp_path / "astrabox"
    package.mkdir()
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(checker, "PACKAGE_ROOT", package)

    def write(relative: str, source: str) -> Path:
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    return checker, write


def test_the_lazy_import_that_lost_a_substitution_is_caught(tree) -> None:
    """The incident's own shape: platform code minting a provider placeholder.

    `prepared_slots` and `provisioning` reach for `slot_model_placeholder`
    inside a function body. A checker that reads only module-level imports
    reports zero violations on this repository and is worthless.
    """
    checker, write = tree
    write(
        "core/service/orchestrator/agent/prepared_slots.py",
        "def compose(slot_id):\n"
        "    from astrabox.providers.open_sandbox.credential_vault import (\n"
        "        slot_model_placeholder,\n"
        "    )\n"
        "    return slot_model_placeholder(slot_id)\n",
    )
    assert checker.collect() == {
        "astrabox/core/service/orchestrator/agent/prepared_slots.py"
        "::slot_model_placeholder"
    }


def test_a_provider_may_use_its_own_credential_module(tree) -> None:
    """The rule is about crossing the seam, not about the machinery existing."""
    checker, write = tree
    write(
        "providers/open_sandbox/executor.py",
        "from astrabox.providers.open_sandbox.credential_vault import (\n"
        "    build_vault_write,\n"
        ")\n",
    )
    assert checker.collect() == set()


def test_one_provider_may_not_borrow_another_providers_vocabulary(tree) -> None:
    """`litellm_extensions` reaching into the sandbox backend's placeholder.

    Two backends cannot both be right about what a placeholder looks like, so
    the one that borrows is coupled to the other's release schedule.
    """
    checker, write = tree
    write(
        "providers/litellm_extensions.py",
        "from astrabox.providers.open_sandbox.credential_vault import (\n"
        "    SANDBOX_PLACEHOLDER_CREDENTIAL,\n"
        ")\n",
    )
    assert checker.collect() == {
        "astrabox/providers/litellm_extensions.py::SANDBOX_PLACEHOLDER_CREDENTIAL"
    }


def test_platform_composing_vendor_credential_models_is_caught(tree) -> None:
    """Hard-coding one backend's idea of what a credential IS."""
    checker, write = tree
    write(
        "core/service/orchestrator/engine/provisioning.py",
        "def build():\n"
        "    from opensandbox.models.sandboxes import (\n"
        "        Credential,\n"
        "        InlineCredentialSource,\n"
        "    )\n"
        "    return Credential, InlineCredentialSource\n",
    )
    assert checker.collect() == {
        "astrabox/core/service/orchestrator/engine/provisioning.py::Credential",
        "astrabox/core/service/orchestrator/engine/provisioning.py"
        "::InlineCredentialSource",
    }


def test_vendor_filesystem_models_are_out_of_scope(tree) -> None:
    """Same shape, different domain, and the gate says so rather than drifting.

    `WriteEntry` and friends are a separate seam question. Silently widening
    this gate to them would make its baseline unreadable and its burn-down
    someone else's problem.
    """
    checker, write = tree
    write(
        "core/service/orchestrator/runtime/sandbox_client.py",
        "from opensandbox.models.filesystem import WriteEntry\n",
    )
    assert checker.collect() == set()


def test_a_network_policy_import_is_not_a_credential_import(tree) -> None:
    """Precision, not a keyword sweep: the module name decides, not the file."""
    checker, write = tree
    write(
        "core/service/orchestrator/runtime/config_resolver.py",
        "from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule\n",
    )
    assert checker.collect() == set()


def test_the_baseline_is_a_ratchet_in_both_directions(tree, tmp_path) -> None:
    """A fixed line must be deleted, or the list stops meaning anything."""
    checker, write = tree
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("astrabox/core/gone.py::slot_model_placeholder\n", encoding="utf-8")
    import unittest.mock as mock

    with mock.patch.object(checker, "BASELINE_PATH", baseline):
        assert checker.read_baseline() == {
            "astrabox/core/gone.py::slot_model_placeholder"
        }
        assert checker.main() == 1


def test_the_live_tree_matches_its_frozen_baseline() -> None:
    checker = load_checker()
    assert checker.collect() == checker.read_baseline()


def test_no_platform_module_mints_a_backends_placeholder() -> None:
    """The incident's own two call sites, closed and held closed.

    `prepared_slots` and `provisioning` composed the substitution table using
    the sandbox backend's placeholder naming. Asserting on the symbol rather
    than on a baseline count keeps the statement readable: no platform or
    engine module may name a backend's placeholder, whatever the arrears total
    happens to be that week.
    """
    checker = load_checker()
    arrears = checker.collect()
    minting = {
        entry
        for entry in arrears
        if entry.rsplit("::", 1)[-1]
        in {"slot_model_placeholder", "SANDBOX_PLACEHOLDER_CREDENTIAL"}
    }
    assert minting == set(), (
        "placeholder vocabulary belongs to astrabox/seams/egress_credentials.py"
    )


def test_the_seam_mints_one_placeholder_per_workload() -> None:
    """Two workloads in one box must never present the same bearer.

    Sharing one would make the egress boundary substitute a sibling's identity
    into this workload's calls — the failure that is silent, because the
    substitution table cannot be read back to notice it.
    """
    from astrabox.seams.egress_credentials import workload_model_placeholder

    first = workload_model_placeholder("slot-aaa")
    second = workload_model_placeholder("slot-bbb")
    assert first != second
    assert workload_model_placeholder("slot-aaa") == first, "must be derivable, not random"
    with pytest.raises(ValueError):
        workload_model_placeholder("   ")
