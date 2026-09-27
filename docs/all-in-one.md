# Run AstraBox in one container

> Start a single-user AstraBox with one `docker run` command.

The all-in-one image runs AstraBox, its PostgreSQL database, the bundled model
gateway and the sandbox lifecycle service in one container, with everything it
stores in one Docker volume. It is the shortest way to try AstraBox on a Linux
machine. For a team, a shared server or team login, install with
[the Quickstart installer](quickstart.md) instead, which runs the same services
with Docker Compose.

Every Session still gets its own sandbox container. The all-in-one container
creates them on the Docker host through the Docker socket, together with two
small proxy containers and two networks, described in
[What runs beside the container](#beside-the-container).

## Requirements

- A Linux host with Docker Engine running as root. The container refuses
  rootless Docker and Podman at startup. Docker Desktop has not been verified
  with this image yet.
- About 20 GB of free disk space for the images. The first start downloads the
  Claude Code sandbox image: about 4 GB to download, about 14 GB unpacked.
- About 2.8 GiB of memory while idle, measured on an x86-64 host: about
  1.6 GiB for the AstraBox container, and about 0.55 GiB for each of the two
  sandboxes, with their egress proxies, that AstraBox keeps ready for the two
  Agents it creates, so that their first conversation does not wait for a
  sandbox to start. Every Agent with prewarming on keeps one ready sandbox,
  and a sandbox may grow to 4 GiB while it works. See
  [Plan capacity for prepared sandboxes](deploy.md#plan-capacity-for-prepared-sandboxes),
  which also explains how to turn prewarming off for an Agent.
- An API key for a model service: Anthropic, DeepSeek, or another
  Anthropic-compatible or OpenAI-compatible service.

## Step 1: Write your model settings

Put the model service's settings in a file that only you can read. Docker
passes them to the container, and they stay out of your shell history.

For Anthropic:

```bash
mkdir -p ~/.config/astrabox
cat > ~/.config/astrabox/model.env <<'EOF'
ANTHROPIC_API_KEY=your-anthropic-api-key
ANTHROPIC_MODEL=your-model-id
EOF
chmod 600 ~/.config/astrabox/model.env
```

For another service, write these lines instead:

| Service | Lines in `model.env` |
|---|---|
| DeepSeek | `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`, `ANTHROPIC_API_KEY=` your DeepSeek key, `ANTHROPIC_MODEL=deepseek-flash` |
| Anthropic-compatible | `ANTHROPIC_BASE_URL=` the service's URL, `ANTHROPIC_API_KEY=` its key, `ANTHROPIC_MODEL=` its model ID |
| OpenAI-compatible | `OPENAI_COMPATIBLE_BASE_URL=` the service's URL, `OPENAI_COMPATIBLE_API_KEY=` its key, `ANTHROPIC_MODEL=openai-compatible/` followed by its model ID |

`ANTHROPIC_MODEL` is the model the seeded Agents use. These are the same
settings the installer writes; [Connect a model](models.md) explains the
gateway routes behind them. To start without a model service, leave out the
`--env-file` line in Step 2 and add routes later under **Management console >
Integrated services > LiteLLM gateway**.

:::note
To use a model server that runs on the same host, add
`--add-host host.docker.internal:host-gateway` to the command in Step 2 and use
`host.docker.internal` as its host name.
:::

## Step 2: Start AstraBox

Run:

```bash
docker run -d --name astrabox --restart unless-stopped \
  -p 127.0.0.1:8088:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v astrabox-data:/data \
  --env-file ~/.config/astrabox/model.env \
  ghcr.io/colton-z/astrabox:0.1.1
```

Then follow the log:

```bash
docker logs -f astrabox
```

The first start initializes the database and downloads the Claude Code sandbox
image, which takes a few minutes and reports its progress in the log. AstraBox
opens the console only after the image is on your computer, so the first
Session does not wait for it. When the log shows this line, open the address
in your browser:

```text
AstraBox is ready at http://127.0.0.1:8088
```

Continue with [Step 2 of the Quickstart](quickstart.md): select an
Environment, create an Agent and start a Session.

:::warning
The console has no login in this setup and is published on your computer's
loopback address only. Do not publish it on another address. To share AstraBox,
use the [Quickstart installer](quickstart.md) with [team login](team-login.md)
and TLS.
:::

The container starts as root only long enough to give its service account
access to the Docker socket and the data volume, then runs every service as
that account. Do not add `--user`, `--group-add`, `--hostname` or
`--network host`: the container refuses the last two, because it finds itself
through the Docker socket by its container ID.

## What runs beside the container {#beside-the-container}

OpenSandbox's network policy for a sandbox names hosts, not ports, so a sandbox
must never be allowed to reach the AstraBox container itself. The container
therefore creates these on the Docker host at startup, named after an
installation identifier stored in the data volume:

| Docker object | Purpose |
|---|---|
| `astrabox-<id>-sandbox-edge` | The one AstraBox address a sandbox may reach. It forwards only the model gateway and the Agent's platform callbacks. |
| `astrabox-<id>-sandbox-dns-edge` | The sandboxes' DNS resolver for the model gateway's private name. |
| `astrabox-<id>-sandbox-edges` | An internal network that joins the two proxies to AstraBox. No sandbox joins it. |
| `astrabox-<id>-platform` | AstraBox's own network. At startup the container moves to it from Docker's default bridge, so other containers on the host cannot reach its ports. The console stays published on `127.0.0.1:8088`. |
| One sandbox container per Session, and one kept ready for each Agent with prewarming on | Created from the Agent's image, with an egress proxy beside it. |

The database and the other internal services listen only inside the AstraBox
container. Sandboxes cannot reach them, other sandboxes, or any AstraBox port
except through the proxy.

The proxies keep running when AstraBox restarts, so sandboxes that are already
running keep their connection. Docker restarts a proxy that crashes. If it
comes back at another address, AstraBox restarts too, and a conversation whose
sandbox still used the old address continues on a new sandbox with its
history.

## Upgrade

Your data, keys and settings are in the `astrabox-data` volume, so an upgrade is
the same command with the new version:

```bash
docker pull ghcr.io/colton-z/astrabox:<new-version>
docker stop astrabox && docker rm astrabox
docker run -d --name astrabox --restart unless-stopped \
  -p 127.0.0.1:8088:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v astrabox-data:/data \
  --env-file ~/.config/astrabox/model.env \
  ghcr.io/colton-z/astrabox:<new-version>
```

Use `docker stop`, not `docker rm -f`: stopping lets the database shut down
cleanly. At startup AstraBox updates its database, then downloads the new
version's sandbox image before it serves. Your Sessions and their history carry
over.

A volume written by a different PostgreSQL major version is refused with a
message instead of being started. A release that changes the major version says
how to move the data.

## Back up and restore

The volume holds everything, including the keys that decrypt the credentials
you stored. Keep the backup as private as the keys themselves. Stop AstraBox so
the copy is consistent:

```bash
docker stop astrabox
docker run --rm -v astrabox-data:/data -v "$PWD":/backup --entrypoint tar \
  ghcr.io/colton-z/astrabox:0.1.1 -czf /backup/astrabox-data.tgz -C /data .
docker start astrabox
```

To restore, extract the backup into a new volume:

```bash
docker volume create astrabox-data-restored
docker run --rm -v astrabox-data-restored:/data -v "$PWD":/backup --entrypoint tar \
  ghcr.io/colton-z/astrabox:0.1.1 -xzf /backup/astrabox-data.tgz -C /data
```

Then remove the old container and run the Step 2 command with
`-v astrabox-data-restored:/data`. Run the restored copy instead of the
original, not beside it: both belong to the same installation, and the second
one to start stops with a message.

Only one container may use a volume at a time. A second container started on
the same volume stops with a message while the first keeps running.

## Uninstall

This removes AstraBox, the proxies and network it created, its sandboxes and
their volumes, and all of its data:

```bash
docker stop astrabox && docker rm astrabox
docker ps -aq --filter label=astrabox.all-in-one.install | xargs -r docker rm -f
docker network ls -q --filter label=astrabox.all-in-one.install | xargs -r docker network rm
docker ps -aq --filter label=opensandbox.io/id | xargs -r docker rm -f
docker ps -aq --filter label=opensandbox.io/egress-sidecar-for | xargs -r docker rm -f
docker volume ls -q --filter label=opensandbox.io/volume-managed-by | xargs -r docker volume rm
docker volume rm astrabox-data
```

The sandbox lines are needed because the lease that ends a sandbox is kept by
the AstraBox container: once it is gone, nothing stops its sandboxes. They
remove every OpenSandbox sandbox and sandbox volume on the Docker host,
including those of any other AstraBox installation there.

## What the all-in-one container does not support

The container runs one fixed setup. At startup it refuses settings that would
change it, names each one, and says what to use instead. These need the
[Compose installation](deploy.md):

- **Team login and access from other computers.** Use Compose with
  [team login](team-login.md) and TLS.
- **An external database, Redis or model gateway.** The container runs its own.
- **More than one machine**, or sandboxes on Kubernetes. See
  [Distributed deployment](deploy-distributed.md).
- **Persistent workspace volumes** and **turning the Credential Vault off**.
- **Moving an all-in-one installation to Compose.** There is no supported
  procedure yet.

Run one all-in-one container per Docker host.

## FAQ

**Q: The container stopped right after it started. What happened?**

A: Run `docker logs astrabox`. A line starting with `FATAL` names the setting or
condition that stopped it, such as a refused setting, a Docker socket the
container cannot open, or a volume that another container is using.

**Q: The log stays at the sandbox image download.**

A: The Claude Code sandbox image is about 4 GB, and the log reports the progress
every 15 seconds. If the download fails, the container stops with the registry's
error; it tries again when Docker restarts it.

**Q: Can I use a registry mirror?**

A: Set `ASTRABOX_IMAGE_PREFIX` in `model.env` to the mirror's prefix, for example
`registry.example.com/astrabox-`. Sandbox images are then pulled from the mirror.
The container pulls without your registry login; for a mirror that needs one,
run `docker pull` for the sandbox image on the host first.
