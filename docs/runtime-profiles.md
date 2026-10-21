# RuntimeProfiles

> Choose the Agent program, sandbox, network, and model connection used by a
> Session.

A RuntimeProfile defines the runtime used by a Session, including its Agent
program, sandbox image, model connection, and network access. Select an
RuntimeProfile available in your AstraBox deployment or create one for a specific
task.

## What a RuntimeProfile Is

A RuntimeProfile is the infrastructure layer beneath a Session:

- **Agent program** - the installed program that powers the Agent, together
  with the sandbox service that runs it.
- **Sandbox image** - the prebuilt image containing the Agent program, system
  packages, language runtimes, accounts, and background services.
- **Connections and controls** - the model service, outbound network access,
  sandbox lifecycle, and optional tracing settings.

When a Session starts, AstraBox creates or assigns a sandbox from the specified
RuntimeProfile template.

## Field Reference

The console presents the following settings.

| Field | Required | Description |
| --- | --- | --- |
| Name | Yes | Unique RuntimeProfile name. It cannot be renamed after creation. |
| Display name | No | Name shown when an Agent author selects the RuntimeProfile. |
| Description | No | What the RuntimeProfile is intended for. |
| Agent program | Yes | The installed program that powers Agents in this RuntimeProfile. |
| Enabled | No | Whether the RuntimeProfile can be selected for an Agent. |
| Sandbox service | No | The service that creates and manages sandboxes. AstraBox includes OpenSandbox support. A RuntimeProfile is refused on a service that cannot create sandboxes under its network access rules. |
| Sandbox image or template | No | The prebuilt sandbox image. When omitted, the selected Agent program uses its deployment default. |
| Network access | No | `Limited` allows required platform connections plus selected destinations; `Unrestricted` allows all outbound destinations. |
| Idle sandbox behavior | No | Terminates an idle sandbox or, when the sandbox service supports snapshots, pauses it for later recovery. |
| Sandbox sharing | No | Gives each conversation its own sandbox or shares one sandbox across the same Agent's conversations. Sharing runs each conversation in an isolated session, and is refused on a sandbox service that cannot. Without sharing, an Agent program that runs as a long-lived process in the sandbox (pi), and an Agent's terminal panel, need a sandbox service that keeps such processes; they are refused on one that does not. |
| Sandbox permissions | No | Grants the system permissions required by the selected Agent program and sandbox-sharing mode. |
| Model connection | No | Selects the model gateway and its connection details for Agents in this RuntimeProfile. |
| Tracing | No | Sends supported OpenTelemetry data from the Agent program to your collector. |

## Runtime Configuration

AstraBox is self-hosted, so a RuntimeProfile does not switch between a vendor's
managed cloud and a self-hosted worker. It selects infrastructure available in
your AstraBox deployment: an Agent program, a sandbox service and image, and a
model connection.

Pre-warming is configured on an Agent, not on its RuntimeProfile. When enabled,
AstraBox prepares the Agent's complete runtime from the selected RuntimeProfile
before a Session needs it. Both tenancy modes use OpenSandbox's official SDK
client pool. Agent tenancy prepares an isolated engine slot in a shared sandbox;
conversation tenancy prepares a complete sandbox before the pool makes it
available for a Session to claim.

For most installations, select an existing RuntimeProfile when creating an Agent.
Deployment operators can create additional RuntimeProfiles when Agents need a
  different Agent program, image, model connection, network rules, or sandbox
lifecycle.

## Preinstalled Packages

Install software in the sandbox image before a Session starts. For example, a
custom image can add system, Python, and Node.js dependencies to an installed
Agent image. Start from the image tag of the AstraBox release you run, because
the platform components inside the image must match the server:

```dockerfile
FROM ghcr.io/colton-z/astrabox-sandbox-claude-code:0.1.9

RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential libssl-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir pandas numpy scikit-learn
RUN npm install -g typescript eslint prettier
```

| Package manager | Image command | Typical use |
| --- | --- | --- |
| apt | `apt-get install` | Debian/Ubuntu system packages |
| pip | `pip3 install` | Python packages |
| npm | `npm install -g` | Node.js packages and CLIs |

> Pin package versions and the base-image tag or digest for production. Building
> dependencies into the image keeps on-demand and pre-warmed sandboxes
> consistent and avoids installing them during Session startup.

## Setup Script

An AstraBox RuntimeProfile does not have a general setup script. Put software,
system accounts, and services required by every sandbox in the image, where they
are available even when the sandbox was prepared before the Session existed.

Keep Agent-specific code in its Git repository and reusable Agent behavior in
its Plugins and Skills. See [Container Reference](container-reference.md) for
the sandbox filesystem layout and image requirements.

## Create a RuntimeProfile

1. Open **Runtime profiles** in the AstraBox console.
2. Select **New runtime profile**.
3. Enter a name and select the Agent program. Configure the sandbox image, model
   connection, network access, and any lifecycle options the RuntimeProfile needs.
4. Select **Create**.

The RuntimeProfile is now available when you create or edit an Agent, as long as it
is enabled and its Agent program supports Agents.

## Read RuntimeProfiles

Open **Runtime profiles** in the AstraBox console to see every RuntimeProfile, including
disabled entries. The list shows its name, Agent program, sandbox service, and
last update time. Select a RuntimeProfile to view its complete configuration.

Agent authors see only enabled RuntimeProfiles that can run Agents. Model and
tracing credentials remain hidden from that selection list.

Disabling a RuntimeProfile prevents new Agent conversations and prewarm refreshes.
The full list (`GET /api/v1/agents` without `page=1`) retains every visible Agent
and returns `can_start_conversation`. It is false when the Agent is disabled or
deleting, or its RuntimeProfile is disabled or missing. The conversation picker
marks these entries **Unavailable** and disables **Start conversation**.
Management and history views retain the definitions. Re-enabling the RuntimeProfile
restores the action for its enabled Agents. This field describes configuration
eligibility, not runtime health; listing neither prepares compute nor checks
credentials. Conversation creation still validates the current configuration.
AstraBox retires its Agents' unclaimed prepared capacity without reclaiming
sandboxes already assigned to Sessions. Managers can still read prepared-runtime
status and delete Agents; the status shows prewarming disabled and zero available
capacity even when the Agent's saved prewarm switch remains on.

## Update a RuntimeProfile

Open the RuntimeProfile, change the required section, and select **Save**.

> Updating a RuntimeProfile does not reconfigure a running sandbox in place. The
> saved configuration is used the next time AstraBox creates, assigns, or
> recreates a sandbox for an Agent that uses the RuntimeProfile. AstraBox also
> reconciles prepared capacity for Agents that depend on the RuntimeProfile.

## Choosing a RuntimeProfile

| Scenario | Recommended configuration |
| --- | --- |
| General development | Use an existing RuntimeProfile for the required Agent program. |
| Data analysis | Use an image with pinned Python and system dependencies, and allow only the required data services. |
| Frontend development | Use an image with the required Node.js toolchain, and allow the package registry if network access is limited. |
| CI/CD integration | Use an image with the required CLIs, connect the required credentials, and allow only the target services. |

## FAQ

**Q: How long do I have to wait after creating a RuntimeProfile before I can use it?**

A: A new RuntimeProfile is immediately available. AstraBox creates or assigns the
actual sandbox when a Session needs it; pre-warming can reduce that startup
time.

**Q: Can I pin package versions?**

A: Yes. Pin them in the sandbox image build and use an immutable image tag or
digest so newly created and pre-warmed sandboxes use the same software.

**Q: How many RuntimeProfiles can I create?**

A: AstraBox does not impose a product limit. Create the RuntimeProfiles your
deployment needs and use clear names to keep them organized.

## Next steps

- [Sessions](sessions.md) - use an Agent to start a task.
- [Defining an Agent](authoring-agents.md) - review Agent configuration.
