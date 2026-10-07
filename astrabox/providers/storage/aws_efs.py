"""Operator-owned EFS filesystems exposed by the official EFS CSI driver.

The provider verifies the backing PVC. The common platform workspace router
owns per-box views; the EFS CSI driver owns network mounts and AWS credentials.
"""

from __future__ import annotations

import asyncio
import re

from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.config.settings import get_settings
from astrabox.providers.storage.mounted_volume import MountedVolumeStorage
from astrabox.seams.storage import StorageMountPlan, register_storage


class AwsEfsStorage(MountedVolumeStorage):
    """Verify a pre-existing EFS CSI claim without owning its cloud lifecycle."""

    name = "aws_efs"

    @staticmethod
    def _configuration() -> tuple[str, str]:
        from astrabox.deploy.sandbox_server import sandbox_runtime

        if sandbox_runtime() != "kubernetes":
            raise ValueError("aws_efs storage requires the Kubernetes runtime and EFS CSI driver")
        runtime = load_astrabox_settings()
        if runtime.workspace_storage_topology != "shared":
            raise ValueError("aws_efs storage requires ASTRABOX_WORKSPACE_STORAGE_TOPOLOGY=shared")
        volume = runtime.sandbox_workspace_volume.strip()
        if not volume:
            raise ValueError("aws_efs storage requires ASTRABOX_SANDBOX_WORKSPACE_VOLUME")
        filesystem = get_settings().efs_file_system_id
        if re.fullmatch(r"fs-[0-9a-f]{8,40}", filesystem) is None:
            raise ValueError(
                "aws_efs storage requires a valid ASTRABOX_EFS_FILE_SYSTEM_ID (fs-...)"
            )
        return volume, filesystem

    def validate_configuration(self) -> None:
        """Reject unsupported deployment shapes before preparing workspaces."""
        self._configuration()

    async def provision_mounts(
        self, assignment_id: str, mounts: tuple[tuple[str, str], ...]
    ) -> StorageMountPlan:
        """Confirm the configured claim actually selects the declared EFS filesystem."""
        volume, filesystem = self._configuration()
        await asyncio.to_thread(self._verify_volume, volume, filesystem)
        return StorageMountPlan(volume_name=volume, mounts=mounts)

    @staticmethod
    def _verify_volume(volume: str, filesystem: str) -> None:
        from kubernetes import client, config
        from kubernetes.client.exceptions import ApiException

        from astrabox.deploy.sandbox_server import kube_namespace, kubeconfig_for_server

        path = kubeconfig_for_server()
        if path is None:
            config.load_incluster_config()
            api_client = client.ApiClient()
        else:
            api_client = config.new_client_from_config(config_file=str(path))
        namespace = kube_namespace()
        target = f"PVC/{namespace}/{volume}"
        try:
            api = client.CoreV1Api(api_client)
            claim = api.read_namespaced_persistent_volume_claim(
                volume, namespace, _request_timeout=30
            )
            claim_meta, claim_spec, claim_status = claim.metadata, claim.spec, claim.status
            if claim_meta is None or claim_spec is None or claim_status is None:
                raise RuntimeError(f"aws_efs {target} has no metadata, spec or status")
            if claim_meta.deletion_timestamp is not None or claim_status.phase != "Bound":
                raise RuntimeError(f"aws_efs {target} must be Bound and not terminating")
            if "ReadWriteMany" not in (claim_spec.access_modes or []):
                raise RuntimeError(f"aws_efs {target} must request ReadWriteMany")
            if claim_spec.volume_mode not in {None, "Filesystem"}:
                raise RuntimeError(f"aws_efs {target} must use Filesystem volume mode")
            if not claim_spec.volume_name:
                raise RuntimeError(f"aws_efs {target} does not identify a bound PV")
            target = f"PV/{claim_spec.volume_name}"
            backing = api.read_persistent_volume(claim_spec.volume_name, _request_timeout=30)
            backing_meta, backing_spec, backing_status = (
                backing.metadata, backing.spec, backing.status
            )
            if backing_meta is None or backing_spec is None or backing_status is None:
                raise RuntimeError(f"aws_efs {target} has no metadata, spec or status")
            if backing_meta.deletion_timestamp is not None or backing_status.phase != "Bound":
                raise RuntimeError(f"aws_efs {target} must be Bound and not terminating")
            reference = backing_spec.claim_ref
            if reference is None or (
                reference.name != volume
                or reference.namespace != namespace
                or reference.uid != claim_meta.uid
            ):
                raise RuntimeError(f"aws_efs {target} does not belong to the configured PVC")
            if "ReadWriteMany" not in (backing_spec.access_modes or []):
                raise RuntimeError(f"aws_efs {target} must provide ReadWriteMany")
            if backing_spec.volume_mode not in {None, "Filesystem"}:
                raise RuntimeError(f"aws_efs {target} must use Filesystem volume mode")
            csi = backing_spec.csi
            if (
                csi is None
                or csi.driver != "efs.csi.aws.com"
                or backing_spec.host_path is not None
                or backing_spec.nfs is not None
            ):
                raise RuntimeError(f"aws_efs {target} must use the official efs.csi.aws.com driver")
            if csi.volume_handle != filesystem:
                raise RuntimeError(
                    f"aws_efs {target} volumeHandle must be exactly {filesystem!r}; "
                    "access-point and prefixed handles are not configured by this provider"
                )
            attributes = csi.volume_attributes or {}
            if any(
                key.lower() == "encryptintransit" and value.lower() != "true"
                for key, value in attributes.items()
            ):
                raise RuntimeError(f"aws_efs {target} must not disable encryption in transit")
            options = {
                option.strip().lower()
                for item in (backing_spec.mount_options or [])
                for option in item.split(",")
            }
            if any(
                option.partition("=")[0] == "notls" or option.startswith("tls=")
                for option in options
            ):
                raise RuntimeError(f"aws_efs {target} must preserve the CSI driver's TLS default")
            if csi.read_only or "ro" in options:
                raise RuntimeError(f"aws_efs {target} must permit workspace writes")
        except ApiException as exc:
            raise RuntimeError(
                f"cannot verify aws_efs {target}: Kubernetes returned {exc.status} {exc.reason}"
            ) from exc
        finally:
            api_client.close()


register_storage(AwsEfsStorage.name, AwsEfsStorage())
