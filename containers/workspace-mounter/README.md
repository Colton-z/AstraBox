# Workspace mount helper

This image runs mergerfs 2.42.0 outside the agent sandbox. It requires Linux
6.9 or newer, root, `/dev/fuse`, mount privileges, and mount propagation from
the helper to the sandbox host. The pinned release archive supports amd64;
building for another architecture fails rather than choosing another binary.

Mount the backing workspace filesystem at `/data`. Mount a separate,
entry-specific host directory at `/views` with bidirectional propagation.
Never expose `/data` or `/views/.control` to the agent sandbox. The sandbox
receives only each view's workspace child directory through its normal volume
interface. Exposing a FUSE root would expose its runtime control file to sandbox
root; the child-directory mount keeps that file outside the sandbox filesystem.

```sh
python3 /opt/astrabox/mergerfs_node.py serve \
  --root /views --data /data \
  --mounts '[["/workspace","prewarm/entry-123/workspace"]]'

python3 /opt/astrabox/mergerfs_node.py bind \
  --root /views --data /data \
  --mounts '[["/workspace","sessions/session-456/workspace"]]' \
  --binding session-456

python3 /opt/astrabox/mergerfs_node.py status --root /views
```

The ordered mapping's first FUSE root is `/views/0`, then `/views/1`, and so on.
For the example, expose only `/views/0/workspace`, never `/views/0` itself.
The branch points to the workspace's parent directory; binding preserves the
leaf name. Status includes the exact `consumer_subpath` for the volume mount.
The controller must complete `bind` before handing the sandbox to the user.
It must not start workspace file operations before that binding completes.
Only the same binding identity and exact target mapping can be repeated.
There is no running-session retarget operation.

`serve --shared` additionally refuses a local backing filesystem. This detects
the filesystem type, not whether other nodes mount the same export; deployment
configuration must establish that identity. A filesystem allowlist is not
evidence of acceptance on every listed storage product.

The helper reads back the actual mergerfs branch and cache/passthrough settings
before reporting JSON `state: READY`. Errors produce JSON on stderr and exit
nonzero. A persisted in-progress binding is never reported ready. Repeating
that exact binding completes its pending updates; restarting the helper mounts
the persisted desired mapping only if no old mount occupies its paths.

The controller must treat helper loss as storage failure for an existing box.
A replacement FUSE mount does not repair handles already held by that box.
An existing or disconnected mount is an error, not permission to mount over it.
SIGTERM unmounts only this process's views and terminates its mergerfs children;
backing workspace files and the durable mapping record are not deleted.

Existing user files are never recursively chowned. Newly needed directories
are created with ordinary root ownership; the existing workspace preparation
flow remains responsible for the configured workload's directory ownership.
Mapping paths reject traversal, mergerfs branch syntax and symlink components.

The image preserves the upstream license under `/usr/share/doc/mergerfs`.
The adapter uses the supplier's [runtime interface][runtime] and
[I/O passthrough configuration][passthrough]; it does not implement a filesystem.

[runtime]: https://trapexit.github.io/mergerfs/2.42.0/runtime_interface/
[passthrough]: https://trapexit.github.io/mergerfs/2.42.0/config/passthrough/
