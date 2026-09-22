"""In-tree bindings of the cloud-provider conformance suites.

Runs the reusable suites in ``astrabox/testing/provider_conformance.py`` over the
built-in OpenSandbox providers, so the structural + capability contract each
suite encodes is proven by a real implementation — exactly like the collection-
and engine-adapter conformance binds. (The suites are STRUCTURAL: live cloud
lifecycle is out of scope, see the module docstring.)
"""

from __future__ import annotations

from astrabox.providers.open_sandbox.sandbox import OpenSandboxSandboxProvider
from astrabox.providers.storage.mounted_volume import MountedVolumeStorage
from astrabox.testing.provider_conformance import (
    SandboxProviderContractSuite,
    StorageProviderContractSuite,
)


class TestOpenSandboxSandboxContract(SandboxProviderContractSuite):
    def make_provider(self):
        return OpenSandboxSandboxProvider()


class TestMountedVolumeStorageContract(StorageProviderContractSuite):
    def make_provider(self):
        return MountedVolumeStorage()
