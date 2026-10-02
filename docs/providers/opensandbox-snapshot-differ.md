# Faster overlayfs snapshots

On Linux containerd nodes using overlayfs, AstraBox's snapshot differ generates
the changed OCI layer from the container's writable layer. This avoids reading
unchanged files across all parent image layers during a sandbox pause. Registry
upload, snapshot reconciliation, and sandbox startup still contribute to the
complete pause and resume time.

The differ uses containerd's supported
[external diff plugin interface](https://github.com/containerd/containerd/blob/v2.3.4/docs/PLUGINS.md#proxy-plugins)
and BuildKit's overlay differ. It does not replace containerd, OpenSandbox's
controller, or the image committer. Install it on every node where a sandbox
can be paused.

## Requirements

- Linux with the overlayfs snapshotter and full copy-up (`metacopy=N`).
- A root system service with access to the same containerd socket and filesystem
  paths used by the sandbox containers.
- The existing containerd `walking` differ retained as a fallback.
- Go 1.26.3 or newer to build the binary; no Go installation is needed at runtime.

The fast path handles one writable overlay layer and gzip OCI output. Other
snapshotters, unrecognized mount options, metadata-only copy-up, directory
redirects, and explicit reproducible timestamps use the existing differ.
Image unpacking also stays with the existing differ. The plugin must remain
available while containerd lists it in its preferred differs; a stopped plugin
is a service failure, rather than an unsupported request.

The layer format retains the native containerd archive semantics, including
file contents, owners, modes, links, deletions, and `security.capability`.
Arbitrary `user.*` extended attributes are not preserved by the underlying
archive writer. The same limitation applies to the native walking differ.

## Build and install

From the repository root:

```bash
cd tools/snapshot-differ
go build -trimpath -o out/astrabox-snapshot-differ ./cmd/astrabox-snapshot-differ
sudo install -D -m 0755 out/astrabox-snapshot-differ /usr/local/libexec/astrabox-snapshot-differ
sudo install -m 0644 astrabox-snapshot-differ.service /etc/systemd/system/astrabox-snapshot-differ.service
sudo systemctl daemon-reload
sudo systemctl enable --now astrabox-snapshot-differ
sudo systemctl is-active astrabox-snapshot-differ
sudo /usr/local/libexec/astrabox-snapshot-differ --check
```

The supplied unit creates a private socket directory, uses a private mount
namespace, and reads the containerd socket at `/run/containerd/containerd.sock`.
If the sandbox node uses a different socket, adjust `--containerd-address` in a
systemd override before starting the service. The process needs mount privileges;
the unit runs as root and must not be run as an unprivileged application service.

Back up the node's containerd configuration. Add the following proxy plugin,
and prepend `astrabox-overlay` to the existing diff service order. Preserve any
other configured differs and keep `walking` available. For the default order:

```toml
[proxy_plugins.astrabox-overlay]
  type = "diff"
  address = "/run/astrabox-snapshot-differ/diff.sock"

[plugins."io.containerd.service.v1.diff-service"]
  default = ["astrabox-overlay", "walking"]
```

Edit an existing diff-service table instead of adding a duplicate TOML table.
Validate the resulting file with `containerd --config <path> config dump`, then
restart containerd during the node's maintenance window. Do not change the
configuration of a different daemon, such as Docker's private containerd.

```bash
sudo systemctl restart containerd
sudo ctr --address /run/containerd/containerd.sock plugins list
```

Both `io.containerd.differ.v1` entries, `astrabox-overlay` and `walking`, must
report `ok`. Check node readiness and verify the complete sandbox journey:

```bash
astrabox verify-opensandbox-snapshots
```

This verifies filesystem snapshots. It does not change the semantics of
[Assistant hibernation](../assistants.md), which persists native session state
and releases compute separately.

## Roll back

Restore the previous diff service order and remove the proxy plugin from the
containerd configuration, validate it, then restart containerd and confirm its
native differ reports `ok`. Only then stop or uninstall the snapshot differ.
Stopping the service while containerd still routes requests to it interrupts
snapshot and unpack operations.
