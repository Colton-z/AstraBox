"""A running container's image is proven against the registry, not its name.

`scripts/runtime_image_identity.py` is what `scripts/e2e_smoke.sh` runs when a
smoke targets an exact Agent. A runtime reports its image as whichever digest
its image store holds — the pushed index, the platform manifest, or the image
config — so the check has to resolve both sides through the registry before it
can say they are the same content.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_DIGEST = "sha256:" + "1" * 64
MANIFEST_DIGEST = "sha256:" + "2" * 64
CONFIG_DIGEST = "sha256:" + "3" * 64
OTHER_MANIFEST_DIGEST = "sha256:" + "4" * 64
OTHER_CONFIG_DIGEST = "sha256:" + "5" * 64
IMAGE = "registry.test:5000/astrabox/sandbox-codex:release-a"
REPOSITORY = "astrabox/sandbox-codex"


def load() -> ModuleType:
    path = REPO_ROOT / "scripts" / "runtime_image_identity.py"
    specification = importlib.util.spec_from_file_location("runtime_image_identity", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules["runtime_image_identity"] = module
    specification.loader.exec_module(module)
    return module


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The module with the registry replaced by one index and two manifests."""

    module = load()
    documents: dict[str, dict[str, Any]] = {
        INDEX_DIGEST: {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"digest": OTHER_MANIFEST_DIGEST,
                 "platform": {"os": "linux", "architecture": "arm64"}},
                {"digest": MANIFEST_DIGEST,
                 "platform": {"os": "linux", "architecture": "amd64"}},
            ],
        },
        MANIFEST_DIGEST: {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": CONFIG_DIGEST},
        },
        OTHER_MANIFEST_DIGEST: {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": OTHER_CONFIG_DIGEST},
        },
    }

    def manifest(*, registry_api: str, repository: str, digest: str) -> dict[str, Any]:
        assert registry_api == "http://registry.test:5000"
        assert repository == REPOSITORY
        module.require(digest in documents, f"registry has no manifest {digest}")
        return documents[digest]

    monkeypatch.setattr(module, "registry_manifest", manifest)
    return module


def evidence(module: ModuleType, observed_image_id: str) -> dict[str, Any]:
    return module.runtime_image_evidence(
        registry_api="http://registry.test:5000",
        expected_image=IMAGE,
        expected_digest=INDEX_DIGEST,
        observed_image_id=observed_image_id,
        operating_system="linux",
        architecture="amd64",
    )


@pytest.mark.parametrize(
    "observed",
    [
        f"docker-pullable://registry.test:5000/{REPOSITORY}@{INDEX_DIGEST}",
        f"containerd://{MANIFEST_DIGEST}",
        CONFIG_DIGEST,
    ],
)
def test_every_image_store_digest_resolves_to_the_expected_content(
    identity: ModuleType, observed: str
) -> None:
    """The three shapes a runtime reports, all naming the release image.

    Docker records the pulled reference with the index digest, containerd
    records the platform manifest, and a Kubernetes imageID may carry the image
    config. A check that compared the reported digest with the pushed one would
    call two of these a different image.
    """

    proof = evidence(identity, observed)
    assert proof["state"] == "PASS"
    assert proof["registry_digest"] == INDEX_DIGEST
    assert proof["runtime_manifest_digest"] == MANIFEST_DIGEST
    assert proof["runtime_config_digest"] == CONFIG_DIGEST
    assert proof["architecture"] == "amd64"
    assert proof["image"] == IMAGE


def test_the_platform_selects_one_manifest_out_of_the_index(identity: ModuleType) -> None:
    """The arm64 entry sits beside the amd64 one; asking for it changes the answer."""

    proof = identity.runtime_image_evidence(
        registry_api="http://registry.test:5000",
        expected_image=IMAGE,
        expected_digest=INDEX_DIGEST,
        observed_image_id=f"containerd://{OTHER_MANIFEST_DIGEST}",
        operating_system="linux",
        architecture="arm64",
    )
    assert proof["runtime_manifest_digest"] == OTHER_MANIFEST_DIGEST
    assert proof["runtime_config_digest"] == OTHER_CONFIG_DIGEST


def test_a_digest_that_resolves_to_other_content_is_refused(identity: ModuleType) -> None:
    """The arm64 manifest is in the same index, and it is still the wrong image."""

    with pytest.raises(identity.RuntimeImageError, match="different content"):
        evidence(identity, f"containerd://{OTHER_MANIFEST_DIGEST}")


def test_an_image_id_from_another_repository_is_refused(identity: ModuleType) -> None:
    """A digest alone is trusted; a digest with a repository must be this one."""

    with pytest.raises(identity.RuntimeImageError, match="different repository"):
        evidence(identity, f"docker-pullable://registry.test:5000/other/image@{INDEX_DIGEST}")


def test_an_expected_image_without_an_exact_tag_is_refused(identity: ModuleType) -> None:
    """A floating reference cannot name the content a turn ran on."""

    with pytest.raises(identity.RuntimeImageError, match="exact tag"):
        identity.runtime_image_evidence(
            registry_api="http://registry.test:5000",
            expected_image=f"registry.test:5000/{REPOSITORY}@{INDEX_DIGEST}",
            expected_digest=INDEX_DIGEST,
            observed_image_id=CONFIG_DIGEST,
            operating_system="linux",
            architecture="amd64",
        )
