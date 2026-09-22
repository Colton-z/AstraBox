# AstraBox CLI Overview

**AstraBox CLI** is a command-line tool designed for developers to simplify the deployment and management of **Agents**. Whether you are working with the maintained local deployment or a remote self-hosted deployment, the `astrabox` command provides the same developer experience.

## Key Advantages

- **Declarative configuration**: Manage Environments and Agents through a single `astrabox.yaml` file—clear, portable, and easy to version-control.
- **Local and remote access**: Use the same resource and Agent commands with the maintained local deployment or a remote self-hosted deployment.
- **One-command startup**: `astrabox up` checks the local prerequisites, starts the maintained Compose deployment, and waits until it is ready.
- **Scriptable output**: Resource, status, and Agent commands provide JSON output and stable exit codes for scripts and CI/CD pipelines.
- **MCP integration**: `astrabox mcp serve` makes the administration operations available as tools to an MCP client.

## Main Commands

The `astrabox` CLI provides commands to manage a deployment and its **Agents**:

### Resource and Agent Commands

| Command | Description |
| :--- | :--- |
| `astrabox init` | Create an `astrabox.yaml` file or export one from a deployment. |
| `astrabox schema` | Show the fields accepted for an Environment or Agent. |
| `astrabox get` | List Agents, Environments, Assistants, Sessions, or remote MCP server configurations. |
| `astrabox diff` | Preview the changes declared in `astrabox.yaml`. |
| `astrabox apply` | Create or update the Environments and Agents declared in `astrabox.yaml`. |
| `astrabox destroy` | Delete the Agents declared in `astrabox.yaml`. |
| `astrabox run` | Send a task to an Agent and stream its reply. |
| `astrabox status` | View the deployment's health, readiness, and endpoint. |

### Deployment and Integration Commands

| Command | Description |
| :--- | :--- |
| `astrabox up` | Start the maintained local deployment and wait until it is ready. |
| `astrabox down` | Stop the maintained local deployment. |
| `astrabox logs` | Read logs from the maintained local deployment. |
| `astrabox mcp serve` | Serve the administration operations as MCP tools over stdio. |

> Want detailed usage for each command? See [Command Reference](./commands.md).

## Three Ways to Use the CLI

**AstraBox CLI** works from a source checkout, as a client of a remote deployment, or as an MCP server for another application.

### 1. Local Deployment

Start and manage the maintained Compose deployment from an AstraBox source checkout.

- **Workflow**: `Source checkout` → `Build Agent sandbox image` → `Start Compose deployment` → `Configure and run Agents`
- **Requirements**: Docker, the source checkout, and the package installed by `make install`.

### 2. Remote Deployment

Point the client commands at any reachable AstraBox deployment.

- **Workflow**: `Installed AstraBox CLI` → `Remote HTTP or HTTPS endpoint` → `Configure and run Agents`
- **Requirements**: The deployment address and, when authentication is enabled, a bearer token or OAuth client credentials.

### 3. MCP Client

Run `astrabox mcp serve` as a stdio MCP server when another application needs to administer the deployment through tools instead of shell commands.

- **Workflow**: `MCP client` → `AstraBox administration tools` → `Local or remote deployment`
- **Benefits**: An AI assistant can inspect schemas, read resources, apply configuration, check status, and run an Agent without parsing terminal output.

## Configuration File (`astrabox.yaml`)

`astrabox.yaml` declares the Environments and Agents that should exist in an **AstraBox** deployment. Preview the changes with `astrabox diff`, then create or update the resources with `astrabox apply`.

```yaml
version: 1

environments:
  - name: default
    engine_kind: <agent-program-id>
    endpoint_provider: <model-connection-id>
    enabled: true

agents:
  - name: researcher
    model: <model-name>
    environment_name: default
    system: |
      Research the requested topic and cite the sources you use.
    enabled: true
```

`engine_kind` is the configuration field that selects an installed Agent program. `endpoint_provider` selects the model-service connection. See [Configuration Reference](./configuration.md) for the file rules and use `astrabox schema environment` and `astrabox schema agent` for the fields available on a deployment.

## Quick Start

In just a few minutes, you can start AstraBox and use the CLI with your first **Agent**.

```bash
# 1. Install AstraBox and build the Agent sandbox image
make install
make build-agent-image

# 2. Start the maintained local deployment
.venv/bin/astrabox up

# 3. Check the deployment
.venv/bin/astrabox status

# 4. Export its current Environments and Agents
.venv/bin/astrabox init --from-deployment

# 5. Preview and apply edits to astrabox.yaml
.venv/bin/astrabox diff -f astrabox.yaml
.venv/bin/astrabox apply -f astrabox.yaml

# 6. Send a task to an Agent
.venv/bin/astrabox run <agent-name> "Summarize this week's repository changes"
```

Connect a model service and create an Agent before the final command. Use the web console, or edit `astrabox.yaml` with fields shown by `astrabox schema`.

### Explore More Features

```bash
# Read complete Agent documents as JSON
astrabox get agents --output json

# Continue an existing Session
astrabox run <agent-name> "Continue the task" --session <session-id>

# Let an MCP client administer the deployment
astrabox mcp serve --endpoint https://astrabox.example.com
```

## Environment Requirements

### ✅ Basic Requirements (All Modes)

- Python 3.12 or later
- The AstraBox package installed
- A reachable AstraBox deployment

### 🐍 Python Development Environment

- From a source checkout, run `make install` to create `.venv` and install the CLI.
- From an installed package, run `astrabox` directly.

### 📜 Scripts and CI/CD

- Use `--output json` with resource, status, and Agent commands.
- Use the documented exit codes to distinguish usage, authentication, connection, and conflict failures.

### 🐳 Local Deployment

- Use an AstraBox source checkout with Docker running.
- Build the Agent sandbox image with `make build-agent-image` before an Agent starts for the first time.

### ☁️ Remote Deployment

- Set the deployment address and its authentication credentials:

  ```bash
  export ASTRABOX_ENDPOINT=https://astrabox.example.com
  export ASTRABOX_TOKEN=<access-token>
  astrabox status
  ```

- OAuth client credentials can be used instead of a bearer token. See [Command Reference](./commands.md#authentication).

## Next Steps

- 📖 [**Command Reference**](./commands.md): Dive into parameters and usage for each CLI command.
- ⚙️ [**Configuration Reference**](./configuration.md): Master the file rules and resource fields in `astrabox.yaml`.
- 🚀 [**Quick Start**](../quickstart.md): Follow the end-to-end tutorial to start AstraBox and create your first Agent.
