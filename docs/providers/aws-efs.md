# AWS EFS workspace storage

The `aws_efs` storage provider connects an operator-owned EFS filesystem through
the official AWS EFS CSI driver. It does not create or delete cloud filesystems,
mount targets or access points. The platform's common mergerfs router still
owns workspace assignment before sandbox delivery; engines do not select the
storage or implement EFS-specific recovery.

This provider currently requires Kubernetes and a statically provisioned,
read-write EFS CSI claim whose `volumeHandle` is exactly the filesystem ID.
The Docker runtime, access-point and subdirectory volume handles, and
`efs:`-prefixed handles are not implemented by this provider and are rejected
explicitly. Local storage and other backing filesystems use `mounted_volume`.

## Configure the backing filesystem

Create an EFS filesystem and mount targets reachable by the sandbox nodes.
Allow TCP 2049 on the mount target's security group only from the intended
clients. Keep the EFS filesystem under operator control, independently of
sandbox and Session lifetimes.

Install the official CSI driver before enabling EFS workspaces. These
instructions pin driver `v3.5.0`, Helm chart `4.5.0`, upstream commit
`978c0b0be261904014ffd68af35a7421bbf439a4`. Static provisioning uses the node
driver; dynamic access-point provisioning is not needed for this integration:

```sh
helm upgrade --install aws-efs-csi-driver aws-efs-csi-driver \
  --repo https://kubernetes-sigs.github.io/aws-efs-csi-driver/ \
  --version 4.5.0 --namespace kube-system --set controller.create=false
```

Create a static PV for the actual filesystem ID, and a PVC in the sandbox
namespace (`ASTRABOX_SANDBOX_SERVER_KUBE_NAMESPACE`). The key PV fields are:

```yaml
spec:
  capacity:
    storage: 20Gi
  volumeMode: Filesystem
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain
  csi:
    driver: efs.csi.aws.com
    volumeHandle: fs-0123456789abcdef0
    volumeAttributes:
      encryptInTransit: "true"
```

The driver encrypts traffic in transit by default; `encryptInTransit: "true"`
states that explicitly. Bind a `ReadWriteMany` PVC to that PV. The declared
capacity is required by Kubernetes; it is not an EFS quota. Wait for the claim
to become `Bound`. Configure AstraBox with:

```dotenv
ASTRABOX_STORAGE_PROVIDER=aws_efs
ASTRABOX_EFS_FILE_SYSTEM_ID=fs-0123456789abcdef0
ASTRABOX_SANDBOX_WORKSPACE_VOLUME=your-bound-efs-claim
ASTRABOX_WORKSPACE_STORAGE_TOPOLOGY=shared
ASTRABOX_WORKSPACE_MOUNTER_IMAGE=your-immutable-workspace-mounter-image
```

Startup rejects `aws_efs` unless the sandbox runtime is Kubernetes, the
topology is `shared`, the workspace volume is set and the filesystem ID has the
`fs-` form. Before supplying each mount plan, the provider reads the live claim
and its PV:

- Both are `Bound`, not terminating, `ReadWriteMany`, and in `Filesystem` mode.
- The PV references that claim by name, namespace and UID.
- The PV uses the `efs.csi.aws.com` driver, and its `volumeHandle` equals the
  configured filesystem ID.
- The PV permits writes. It keeps encryption in transit: `encryptInTransit`
  other than `"true"`, or a `notls` or `tls=` mount option, is rejected.

A local volume with the same claim name does not satisfy this check. The
platform then uses its normal workspace router and OpenSandbox mount interface.

## Filesystem behavior and verification

EFS does not support user extended attributes. The router preserves the
underlying filesystem's result instead of emulating them. The mergerfs control
entry is separate from file xattrs and is not exposed in the sandbox workspace.
File operations required by workspace use include read/write, atomic rename,
mode changes, symbolic links and advisory locks.

AstraBox stores the Agent program's native session data in the platform
database, so conversation recovery does not depend on EFS. Releasing a
workspace assignment deletes only the helper Pod and the view PV/PVC that
AstraBox created for it. The backing claim, its PV and the EFS data stay in
place.

A successful driver installation or a `Bound` claim shows only that Kubernetes
can mount the filesystem. Verify workspace persistence separately, with
sandboxes on different nodes, and record those results.

References: [AWS CSI static provisioning](https://github.com/kubernetes-sigs/aws-efs-csi-driver/blob/978c0b0be261904014ffd68af35a7421bbf439a4/examples/kubernetes/efs/static_provisioning/README.md),
[driver installation](https://github.com/kubernetes-sigs/aws-efs-csi-driver/blob/978c0b0be261904014ffd68af35a7421bbf439a4/docs/install.md),
[EFS network access](https://docs.aws.amazon.com/efs/latest/ug/network-access.html),
and [unsupported filesystem features](https://docs.aws.amazon.com/efs/latest/ug/limits.html).
