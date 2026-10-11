"""The OpenSandbox backend's workspace route: host mergerfs views over the storage medium.

``mergerfs`` is the route (:class:`~astrabox.seams.storage.WorkspaceRoute`);
``_mergerfs_docker`` and ``_mergerfs_kubernetes`` place the privileged helper
beside the runtime the bundled lifecycle server drives; ``mergerfs_node`` is
the helper's own program, copied into the workspace-mounter image and run
there with no application dependencies.
"""
