# Container reference

> Understand the workspace, software, resources, and persistence provided by
> an Agent sandbox.

An AstraBox Environment runs each Agent in a container. Because AstraBox is
self-hosted, the operating system, commands, and resources are determined by
the sandbox image and deployment.

In the bundled Agent images, you can rely on the following behavior:

- The Agent program starts in `/workspace` by default.
- Software required by every sandbox is built into the image.
- AstraBox does not run a general setup script when a Session starts.
- Workspace file retention depends on the sandbox lifecycle and optional persistent storage.

## Runtime and operating system

AstraBox does not promise one operating-system release across every Agent
image. CPU architecture, kernel, and container runtime can also vary by
deployment.

Inspect the actual environment inside a Session:

```bash
cat /etc/os-release
uname -m
uname -r
```

> If you use native binaries, detect the architecture inside the Session or
> provide builds for every required architecture.

## Tools in the current image

Each sandbox image contains the selected Agent program and the services it
needs to run in AstraBox. The exact system commands and language versions
depend on the image and can change when that image is updated.

Inspect the versions a task depends on inside the Session:

```bash
git --version
python3 --version
node --version
```

If a task needs an exact version, pin it in a custom image and publish that
image with an immutable tag or digest.

## Working directory

The default working directory for Agent programs is:

```text
/workspace
```

Bundled Agent images set `WORKSPACE` to `/workspace`, and commands start there
when no other directory is provided. Put repositories, uploaded files, and
generated output in this workspace.

The Session Files panel and API operate directly on this workspace; AstraBox
does not create a separate File resource or use a `mount_path`. See
[Files](files.md).

Platform runtime logs and transport buffers belong outside this directory.
Isolated engine services keep them in their private Home; their launch-failure
diagnostics read the same locations. The bundled AIO browser remains disabled,
and its startup download directory is configured as
`/tmp/astrabox-browser-downloads`, not a folder in the user workspace.
Custom images must likewise keep service logs and caches out of Workspace.

Engine instruction files are task context, not operational artifacts. Pi and
DeepSeek Harness write configured Agent instructions to Workspace `AGENTS.md`,
replacing that file. Leave Agent instructions unset to keep the repository's
own instruction file.

## Installing extra software

Add software needed by every Session to the sandbox image. Start from the
bundled image for the selected Agent program, at the tag of the AstraBox
release you run:

```dockerfile
FROM ghcr.io/colton-z/astrabox-sandbox-claude-code:0.1.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client redis-tools \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir 'pandas==2.2.3'
RUN npm install -g 'typescript@5.8.3'
```

| Package manager | Installation behavior |
| --- | --- |
| apt | Install system packages with `apt-get install` |
| pip | Install Python packages with `pip3 install` |
| npm | Install Node.js packages with `npm install -g` |

Select the resulting image under **Sandbox image or template** in the
Environment console. Pin the base image and dependencies for production.

AstraBox does not install a general package list or run a general setup script
when a Session starts. Accounts, Agent programs, and background services
required by the sandbox must also be built into the image so on-demand and
pre-warmed sandboxes behave the same way.

### Custom base images and startup

Extending the matching bundled image is the supported integration path. It
retains the selected engine, OpenSandbox execution service, AstraBox runtime
helpers, workload accounts, and startup configuration. Install additional software
without removing these components or replacing their startup services.

Using an unrelated base image requires reproducing that engine image's
Dockerfile contract; AstraBox does not currently distribute a standalone
runtime installer for arbitrary base images. Copying a runner script alone
does not install its dependencies, execution service, or account setup.

The platform explicitly supplies the engine's sandbox startup command:
Claude Code and Pi use `/opt/astrabox/boot.sh`, which starts their services
and invokes AIO's `/opt/gem/run.sh`; Codex and DeepSeek Harness use
`/opt/gem/run.sh` with their image-installed Supervisor services.
Changing only the derived image's Docker `ENTRYPOINT` does not change
that platform-supplied command.

## Resources and timeouts

AstraBox's current OpenSandbox create recipe gives every sandbox limits of `4`
CPU and `4Gi` memory, and scheduling requests of `200m` CPU and `768Mi` memory.
Cold creation and prepared capacity use the same recipe. These values are
platform settings, not per-Session Environment options; disk capacity and
execution deadlines still depend on the sandbox service and deployment.

If a task has minimum requirements, validate them in the target Environment.
Processes can be terminated or writes can fail when memory or disk is exhausted,
and long tasks must account for the deployment's execution deadlines.

## File persistence

- Files remain between turns while the same sandbox is retained.
- Without persistent workspace storage, `Terminate` deletes an idle sandbox
  and its local workspace when the configured idle period ends.
- With persistent workspace storage configured, the same conversation's
  working files survive sandbox replacement. See [deployment](deploy.md).
- `Pause` preserves the sandbox filesystem only when the selected sandbox
  service supports and verifies snapshots. The next turn restores those files
  before the Agent program continues. This restores files, not process memory;
  mounted persistent workspace storage has its own lifecycle.
- When a sandbox is recreated without a snapshot or persistent workspace,
  its previous local working files are not retained.

Native conversation state is saved separately in AstraBox's database. Restoring
that state does not require a persistent workspace volume.

> Treat the container filesystem as a working directory. Download important
> files through the Session Files panel or API, commit and push code changes,
> or write durable output to another external store.

## Execution user and environment variables

The execution user and the values of `HOME`, `USER`, `SHELL`, and `LANG` can
vary by Agent program, sandbox-sharing mode, or custom image. Do not make
scripts depend on a specific UID or assume that system directories are always
writable.

Inspect the current Session when needed:

```bash
id
whoami
printf 'HOME=%s\nUSER=%s\nSHELL=%s\nLANG=%s\nWORKSPACE=%s\n' \
  "$HOME" "${USER:-}" "${SHELL:-}" "${LANG:-}" "${WORKSPACE:-}"
```

Model credentials and credentials from linked Vaults are supplied according to
the Environment and Credential Vault configuration. Never print secrets in
logs or task output.

## Related documents

- [Environments](environments.md) - Environment configuration
- [Files](files.md) - work with files in a Session workspace
- [Credential Vaults](credentials.md) - credentials and injection
