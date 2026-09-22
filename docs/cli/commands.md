---
title: CLI Command Reference
---

# CLI Command Reference

**AstraBox CLI** is the core tool for interacting with an **AstraBox** deployment. It provides a complete set of commands designed to simplify and automate deployment startup, resource configuration, and **Agent** use. Whether you are running AstraBox locally, administering a remote deployment, or sending an Agent a task, **AstraBox CLI** provides the same command surface.

Each command below includes its purpose, parameters/options, examples, and operating notes.

## Command Overview

**AstraBox CLI** follows the standard format: `astrabox <command> [arguments] [options]`.

| Command | Description | Core Use Cases |
| :--- | :--- | :--- |
| `init` | **Initialize configuration**: Create `astrabox.yaml`, or export a deployment into one. | Start configuration as code; bring existing resources into a file. |
| `schema` | **View resource fields**: Read the Agent or Environment field definitions from a deployment. | Author valid configuration; inspect candidate values. |
| `get` | **View resources**: List a collection or one member. | Inspect Agents, Environments, Assistants, Sessions, and remote MCP Server configurations. |
| `diff` | **Preview changes**: Show what `apply` would change without writing. | Review a configuration before applying it. |
| `apply` | **Apply configuration**: Create or update declared Environments and Agents. | Configure a deployment from a file; CI/CD integration. |
| `run` | **Run an Agent**: Send a task and stream the reply. | Use an Agent from a terminal or script. |
| `status` | **View status**: Probe the deployment's health and readiness endpoints. | Monitor a local or remote deployment. |
| `destroy` | **Clean up resources**: Delete the Agents declared in a file. | Remove declared Agents with explicit confirmation. |
| `up` | **Start locally**: Start the maintained Compose deployment. | Run AstraBox from a source checkout. |
| `down` | **Stop locally**: Stop the maintained Compose deployment. | Stop services or delete their volumes. |
| `logs` | **View local logs**: Read or follow Compose service logs. | Diagnose a local deployment. |
| `mcp serve` | **Serve MCP tools**: Expose administration commands over stdio. | Let an MCP client administer AstraBox. |
| `serve` | **Run the API server**: Start the FastAPI application with Uvicorn. | Local development and operator workflows. |
| `verify-opensandbox-snapshots` | **Verify snapshots**: Exercise pause and resume against OpenSandbox. | Deployment verification by operators. |

---

## `astrabox init`

`astrabox init` guides you through creating an `astrabox.yaml` file. It supports both “start from a skeleton” and “export an existing deployment”, significantly improving configuration efficiency.

### Modes

1. **Skeleton Mode**: Create an annotated file without contacting a deployment. Suitable for developers starting configuration from scratch.
2. **Export Mode**: Project the Environments and Agents already held by a deployment into a file that `astrabox apply` accepts.

### Syntax

```bash
# Skeleton mode: create an annotated configuration file
astrabox init [options]

# Export mode: export an existing deployment
astrabox init --from-deployment [options]
```

### Core Parameter

- `--file`, `-f` (optional):
  - **Description**: Where to write the configuration file.
  - **Default**: `astrabox.yaml`.
  - **Constraints**: The command refuses to overwrite an existing file unless `--force` is present.

### Skeleton Mode Options

| Option | Description | Example |
| :--- | :--- | :--- |
| `--file`, `-f` | Write the skeleton to another path. | `--file config/astrabox.yaml` |
| `--force` | Overwrite the target if it already exists. | `--force` |

### Export Mode Options

| Option | Description | Example |
| :--- | :--- | :--- |
| `--from-deployment` | Export a running deployment instead of writing the skeleton. | `--from-deployment` |
| `--endpoint` | Set the deployment base URL. | `--endpoint https://astrabox.example.com` |
| `--token` | Send a bearer token to the deployment. | `--token <access-token>` |

### Common Options

| Option | Description | Default |
| :--- | :--- | :--- |
| `--output`, `-o` | Output format: `table` or `json`. | `table` |
| `--force` | Overwrite the target file. | Disabled |

### Examples

#### Skeleton Mode

```bash
# Example 1: create astrabox.yaml in the current directory
astrabox init

# Example 2: create the file at a specified path
astrabox init --file config/astrabox.yaml

# Example 3: overwrite an existing file
astrabox init --force

# Example 4: shorthand for a specified path
astrabox init -f deploy/astrabox.yaml
```

#### Export Mode

```bash
# Example 5: export the default local deployment
astrabox init --from-deployment

# Example 6: export a remote deployment
astrabox init --from-deployment \
  --endpoint https://astrabox.example.com

# Example 7: export to a specified path
astrabox init --from-deployment \
  --file deploy/astrabox.yaml

# Example 8: export with a bearer token
astrabox init --from-deployment \
  --endpoint https://astrabox.example.com \
  --token <access-token>

# Example 9: export and return the result as JSON
astrabox init --from-deployment --output json
```

**Best practices**

- Resolve duplicate Agent names before export. `init --from-deployment` refuses an ambiguous name and leaves the output file untouched rather than writing a document `apply` cannot identify.

- **Start from a skeleton**: For a new deployment, start with the annotated file and read the accepted fields with `astrabox schema`.
- **Use export mode**: When a deployment already has working Environments and Agents, export it instead of recreating those resources by hand.
- **Review before overwriting**: Use a different `--file` path or version control before combining `--force` with an existing file.

### Output

#### Skeleton mode output

After running a skeleton-mode command, you will see output similar to:

```text
{
  "agents": 1,
  "environments": 1,
  "file": "astrabox.yaml",
  "from_deployment": false
}
wrote astrabox.yaml
```

The generated file contains `version: 1`, one example Environment, and one example Agent. Use `astrabox schema environment` and `astrabox schema agent` to replace the example fields with values accepted by the deployment.

#### Export mode output

After running an export-mode command, you will see output similar to:

```text
{
  "agents": 2,
  "environments": 1,
  "file": "astrabox.yaml",
  "from_deployment": true
}
wrote astrabox.yaml
```

### Export mode deep dive

Export mode lets you move a deployment configured in the web console into an `astrabox.yaml` file without recreating it by hand.

#### How it works

1. **Read the field schemas**: Reads the Agent and Environment authoring schemas from the deployment.
2. **Read the resources**: Reads the Environments and Agents the deployment holds.
3. **Project writable fields**: Keeps only fields declared by the corresponding authoring schema.
4. **Write the file**: Writes a `version: 1` document that `astrabox diff` and `astrabox apply` accept.

#### What the export does

The generated file is responsible for:

- **Declaring Environments first**: A fresh deployment can create an Environment before an Agent refers to it.
- **Excluding server-managed fields**: IDs, versions, ownership, and timestamps are not written into the file.
- **Preserving stored secrets**: Masked secret values can be applied unchanged to keep the secret already stored by the deployment.

#### Requirements for export mode

```bash
# AstraBox CLI must be installed
astrabox --help

# The deployment must be reachable
astrabox status
```

#### FAQ

**Q: Can I create a file without a running deployment?**

A: Yes. `astrabox init` writes the annotated skeleton and contacts nothing.

**Q: Why did the command refuse to write the file?**

A: The target already exists. Choose another path or pass `--force` after reviewing the existing file.

**Q: Can the exported file be applied to another deployment?**

A: Yes, when the target deployment accepts the same resource fields and candidate values. Check it with `astrabox schema` and `astrabox diff` first.

---

## `astrabox schema`

`astrabox schema` reads the resource fields that a deployment accepts when writing an Agent or Environment. The schema includes every field's key, type, whether it is required, and its candidate values where it has a fixed set.

### Usage

```bash
# Read the Agent field schema
astrabox schema agent

# Read the Environment field schema
astrabox schema environment
```

### Two resource schemas

#### 🎯 Agent schema

The Agent schema describes the settings that define what an Agent uses and how it behaves, including its name, model, Environment, system prompt, MCP Servers, Skills, Plugins, repository, and other supported settings.

```bash
astrabox schema agent
```

#### ⚡ Environment schema

The Environment schema describes the Agent program, model-service connection, credentials, and defaults available to Agents that use the Environment.

```bash
astrabox schema environment
```

### Main parameters

#### Resource kind

| Parameter | Description | Allowed values |
| :--- | :--- | :--- |
| `kind` | Resource type whose authoring schema should be shown. | `agent`, `environment` |

#### Candidate values

When a field accepts only a fixed set, the table output shows those values in the `ENUM` column. The JSON output includes the complete field descriptors, including nested item schemas.

```bash
# Human-readable field table
astrabox schema environment

# Complete machine-readable schema
astrabox schema environment --output json
```

The candidate values are supplied by the deployment. For example, `engine_kind` selects an installed Agent program and `endpoint_provider` selects a model-service connection type.

### Control options

| Option | Description | Default |
| :--- | :--- | :--- |
| `--endpoint` | Deployment base URL. | `ASTRABOX_ENDPOINT`, then the maintained local address |
| `--token` | Bearer token. | `ASTRABOX_TOKEN`, then OAuth client credentials |
| `--output`, `-o` | Output format: `table` or `json`. | `table` |

### Examples

#### Example 1: inspect Agent fields

```bash
astrabox schema agent
```

The table lists the field key, type, required status, and candidate values. Use those keys inside an item under `agents:` in `astrabox.yaml`.

#### Example 2: inspect Environment fields

```bash
astrabox schema environment
```

Use those keys inside an item under `environments:` in `astrabox.yaml`.

#### Example 3: read nested field definitions

```bash
astrabox schema agent --output json
```

JSON output includes field descriptions and item schemas that do not fit in the table.

#### Example 4: inspect a remote deployment

```bash
astrabox schema agent \
  --endpoint https://astrabox.example.com
```

#### Example 5: use a bearer token

```bash
astrabox schema environment \
  --endpoint https://astrabox.example.com \
  --token <access-token>
```

#### Example 6: use environment variables

```bash
export ASTRABOX_ENDPOINT=https://astrabox.example.com
export ASTRABOX_TOKEN=<access-token>
astrabox schema agent
```

#### Example 7: CI/CD integration

```bash
astrabox schema agent --output json > agent-schema.json
astrabox schema environment --output json > environment-schema.json
```

### Configuration validation

`astrabox schema` does not validate a local file. `astrabox diff -f astrabox.yaml` parses the document, checks its top-level structure, reads these schemas, and validates every declared field before it sends any write.

### Best practices

1. **Read the schema before authoring**: Start with the deployment you plan to configure.
2. **Use JSON for automation**: The JSON form preserves the complete field descriptors.
3. **Keep candidate values out of scripts**: Read fixed sets from the target deployment instead of assuming every deployment has the same Agent programs or model connections.
4. **Preview the file**: Run `astrabox diff` after changing `astrabox.yaml`.
5. **Keep secrets out of source control**: Prefer the web console or deployment secret management for credentials; exported secret values are masked.

---

## `astrabox get`

`astrabox get` lists the resources held by a deployment. It can return a collection or one member matched by name and then by ID.

### Usage

```bash
astrabox get <kind> [name] [options]
```

### Parameter description

| Parameter/Option | Description | Required |
| :--- | :--- | :--- |
| `kind` | Collection to read. | Yes |
| `name` | Return one member, matched by name and then by ID. | No |
| `--endpoint` | Deployment base URL. | No |
| `--token` | Bearer token. | No |
| `--output`, `-o` | Output format: `table` or `json`. | No |

### Resource collections

#### Supported kinds

| Kind | What it returns |
| :--- | :--- |
| `agents` | Agents that can start conversations and perform tasks. |
| `environments` | Reusable Agent-program and model-service settings. |
| `assistants` | Assistants exposed by the deployment. |
| `sessions` | Conversations and their current state. |
| `mcp-servers` | Remote MCP Server configurations stored by the deployment. |

#### Complete example

```bash
# List every Agent
astrabox get agents

# Read one Agent by name
astrabox get agents researcher

# Read one Session by ID
astrabox get sessions <session-id>

# Return complete Environment documents
astrabox get environments --output json
```

#### Use cases

- **Inspect before editing**: Read the current resource before changing it in the web console or `astrabox.yaml`.
- **Find IDs for automation**: JSON output includes the complete stored documents.
- **Monitor conversations**: List Sessions to see their IDs and current states.
- **Discover model settings**: Read Environments before choosing one for an Agent.

### Output

The default table displays the identifying and commonly used fields for the selected collection. `--output json` returns the complete documents.

```text
NAME        MODEL              ENVIRONMENT_NAME  ENABLED  AGENT_ID
researcher  <model-name>       default           true     <agent-id>
```

### Examples

```bash
# Example 1: list Agents
astrabox get agents

# Example 2: list Environments
astrabox get environments

# Example 3: list Assistants
astrabox get assistants

# Example 4: list Sessions
astrabox get sessions

# Example 5: list remote MCP Server configurations
astrabox get mcp-servers

# Example 6: read one member by name
astrabox get agents researcher

# Example 7: read one member by ID
astrabox get sessions <session-id>

# Example 8: query a remote deployment
astrabox get agents --endpoint https://astrabox.example.com

# Example 9: machine-readable output
astrabox get agents --output json
```

### Notes

- A missing named member exits with code 1.
- If several members match the supplied name, JSON output returns the matching list instead of guessing which one was intended.
- Table output is intentionally compact; use JSON when a script needs nested settings or server-managed fields.
- `get` only reads resources and sends no writes.

---

## `astrabox diff`

`astrabox diff` previews what applying an `astrabox.yaml` file would change. It performs the same reads as `astrabox apply` and sends no writes.

### Usage

```bash
astrabox diff --file <path> [options]
```

### Parameter Description

| Parameter/Option | Description | Required |
| :--- | :--- | :--- |
| `--file`, `-f` | Path to `astrabox.yaml`. | Yes |
| `--endpoint` | Deployment base URL. | No |
| `--token` | Bearer token. | No |
| `--output`, `-o` | Output format: `table` or `json`. | No |

### Comparison Process

1. **Load configuration**: Read and parse the YAML document.
2. **Validate structure**: Require `version: 1`, known top-level keys, and correctly shaped resource lists.
3. **Read field schemas**: Validate each Agent and Environment against the target deployment.
4. **Match resources**: Match declared resources to stored resources by name.
5. **Report actions**: Show whether each resource would be created, updated, or left unchanged.

`diff` stops on an ambiguous Agent name instead of selecting one. This is the same conflict behavior used by `apply`.

### Usage Examples

```bash
# Example 1: preview the default file
astrabox diff --file astrabox.yaml

# Example 2: shorthand
astrabox diff -f astrabox.yaml

# Example 3: preview against a remote deployment
astrabox diff -f astrabox.yaml \
  --endpoint https://astrabox.example.com

# Example 4: return machine-readable results
astrabox diff -f astrabox.yaml --output json
```

### After Comparison

The default table contains one row per declared resource:

```text
KIND         NAME        ACTION     FIELDS
environment  default     unchanged
agent        researcher  update     model, system
```

- `create`: The resource does not exist and `apply` would create it.
- `update`: The resource exists and the listed fields differ.
- `unchanged`: The resource already matches the declaration.

Review the result, then run `astrabox apply -f astrabox.yaml` to send the writes.

---

## `astrabox apply`

`astrabox apply` creates or updates every Environment and Agent declared in an `astrabox.yaml` file.

### Usage

```bash
astrabox apply --file <path> [options]
```

### Parameter Description

| Parameter/Option | Description | Required |
| :--- | :--- | :--- |
| `--file`, `-f` | Path to `astrabox.yaml`. | Yes |
| `--dry-run` | Report what would change without sending a write. | No |
| `--endpoint` | Deployment base URL. | No |
| `--token` | Bearer token. | No |
| `--output`, `-o` | Output format: `table` or `json`. | No |

### Execution Flow

```
Load and validate astrabox.yaml
        ↓
Read resource schemas and current resources
        ↓
Apply Environments before Agents
        ↓
Create or update each declared resource
        ↓
Return one action for every declaration
```

- **Environment writes are complete replacements**: Declare all required Environment fields. Starting from `astrabox init --from-deployment` avoids accidentally omitting existing settings.
- **Agent writes are updates**: The command sends the declared fields and carries the stored version so a concurrent edit is refused instead of overwritten.
- **No implicit deletion**: A resource omitted from the file remains on the deployment. Use `astrabox destroy` to remove declared Agents.

### Usage Examples

```bash
# Example 1: apply the default file
astrabox apply --file astrabox.yaml

# Example 2: shorthand
astrabox apply -f astrabox.yaml

# Example 3: preview through the apply command
astrabox apply -f astrabox.yaml --dry-run

# Example 4: apply to a remote deployment
astrabox apply -f astrabox.yaml \
  --endpoint https://astrabox.example.com

# Example 5: machine-readable result for CI/CD
astrabox apply -f astrabox.yaml --output json
```

### Why Use apply

- ✅ **One declaration**: Keep related Environments and Agents in the same file.
- ✅ **Dependency order**: Environments are created or updated before Agents refer to them.
- ✅ **Conflict protection**: A stale Agent version or ambiguous name fails instead of overwriting or guessing.
- ✅ **Repeatable result**: A second apply reports unchanged resources when the declaration already matches.
- ✅ **Automation support**: JSON output and stable exit codes work in scripts and CI/CD pipelines.

---

## `astrabox run`

`astrabox run` starts a conversation with an Agent, sends one task, and streams the reply until the Session stream closes. It can also continue an existing Session.

### Usage

```bash
astrabox run <agent> <task> [options]
```

### Parameter Description

| Parameter/Option | Description | Required | Default |
| :--- | :--- | :--- | :--- |
| `agent` | Agent name or Agent ID. The positional value remains required when `--session` is used. | Yes | — |
| `task` | Task to send to the Agent. | Yes | — |
| `--session` | Continue an existing Session instead of starting a conversation. | No | Starts a conversation |
| `--timeout` | Deadline in seconds for the whole run, including sandbox readiness and the turn. | No | `900` |
| `--endpoint` | Deployment base URL. | No | Local maintained address |
| `--token` | Bearer token. | No | Environment or OAuth credentials |
| `--output`, `-o` | Output format: `table` or `json`. | No | `table` |

### Usage Examples

#### Example 1: Send a task directly (simplest)

```bash
astrabox run researcher "Summarize this week's repository changes"
```

The command resolves `researcher` by name or ID, creates a conversation, waits until its sandbox can accept a turn, and then streams the reply.

#### Example 2: Send a longer task

```bash
astrabox run researcher \
  "Compare the two proposals in docs/ and list the evidence for each conclusion"
```

#### Example 3: Use a remote deployment

```bash
astrabox run researcher "Review the open pull requests" \
  --endpoint https://astrabox.example.com
```

#### Example 4: Use a bearer token

```bash
astrabox run researcher "Prepare the release notes" \
  --endpoint https://astrabox.example.com \
  --token <access-token>
```

#### Example 5: Continue an existing Session

```bash
astrabox run researcher "Continue from the previous result" \
  --session <session-id>
```

The `agent` positional value is still required by the command syntax, but the existing Session selects the conversation that receives the task.

#### Example 6: Return one JSON result

```bash
astrabox run researcher "Return the latest status" --output json
```

In JSON mode, the reply is accumulated into the result object so stdout remains one valid JSON document.

#### Example 7: Set a shorter deadline

```bash
astrabox run researcher "Run the smoke check" --timeout 120
```

### Execution Output

With the default table output, response text is printed as it arrives. When the stream closes, the command reports the Session ID and whether the Agent is waiting for an answer.

```text
Reviewing the repository now...
The release checklist has three remaining items.
session <session-id>
```

With `--output json`, the result contains the accumulated `text`, `frames`, `errors`, `session_id`, and `pending_interaction` fields.

### Notes

1. **Sandbox readiness is included**: A new conversation exists before its sandbox is ready. The command waits for the Session to reach `READY` before sending the task.
2. **One deadline covers the run**: `--timeout` covers both the readiness wait and the streamed turn.
3. **A closed stream may mean a question**: The command reads the Session again and reports `pending_interaction` when the Agent is waiting for an answer.
4. **Pending questions are answered elsewhere**: Answer in the web console or through the [HTTP API](../api.md).
5. **Streaming differs by output format**: Table output prints response text live; JSON output buffers it so the JSON document remains parseable.
6. **Agent errors affect the exit code**: If the streamed result contains errors, the command exits with code 1.

---

## `astrabox status`

`astrabox status` probes a deployment's health and readiness endpoints. It works with local and remote deployments and does not require a source checkout.

### Usage

```bash
astrabox status [options]
```

### Parameter Description

| Option | Description | Default |
| :--- | :--- | :--- |
| `--endpoint` | Deployment base URL. | `ASTRABOX_ENDPOINT`, then `http://127.0.0.1:$ASTRABOX_SERVER_HOST_PORT` |
| `--token` | Bearer token. | `ASTRABOX_TOKEN`, then OAuth client credentials |
| `--output`, `-o` | Output format: `table` or `json`. | `table` |

### Output Example

#### 🏠 Local Deployment

```bash
astrabox status
```

```json
{
  "detail": "ready",
  "endpoint": "http://127.0.0.1:8088",
  "healthy": true,
  "ready": true
}
```

#### ☁️ Remote Deployment

```bash
astrabox status --endpoint https://astrabox.example.com
```

```json
{
  "detail": "ready",
  "endpoint": "https://astrabox.example.com",
  "healthy": true,
  "ready": true
}
```

### Status Description

| Field | Description |
| :--- | :--- |
| `endpoint` | Deployment address that was probed. |
| `healthy` | Whether `/healthz` answered successfully. |
| `ready` | Whether `/readyz` reported that the deployment can serve traffic. |
| `detail` | Health detail when unhealthy; otherwise readiness detail. |

If no deployment answers, the command exits with code 4 and includes the endpoint and probe details in JSON output.

### Usage Examples

```bash
# Example 1: view the default local deployment
astrabox status

# Example 2: view a remote deployment
astrabox status --endpoint https://astrabox.example.com

# Example 3: use environment configuration
ASTRABOX_ENDPOINT=https://astrabox.example.com astrabox status

# Example 4: return machine-readable output
astrabox status --output json
```

---

## `astrabox destroy`

`astrabox destroy` deletes every Agent declared in an `astrabox.yaml` file. It requires explicit acknowledgement and reports declared Environments as retained.

### Usage

```bash
astrabox destroy --file <path> --yes [options]
```

### Parameter Description

| Parameter/Option | Description | Required |
| :--- | :--- | :--- |
| `--file`, `-f` | Path to `astrabox.yaml`. | Yes |
| `--yes` | Acknowledge that the command deletes live Agents. | Yes |
| `--endpoint` | Deployment base URL. | No |
| `--token` | Bearer token. | No |
| `--output`, `-o` | Output format: `table` or `json`. | No |

### Safety Confirmation

The command has no interactive prompt. It refuses to run unless `--yes` is present:

```bash
astrabox destroy -f astrabox.yaml --yes
```

Review the file and run `astrabox diff` or `astrabox get agents` before acknowledging deletion.

### What Will Be Deleted

#### Agents

Every item under `agents:` is matched by name and deleted when present. A declared Agent that is already absent is reported as `absent`.

#### Environments

Items under `environments:` are reported as `retained`. AstraBox exposes no Environment delete route, so the CLI does not claim to delete them.

Resources omitted from the file are not touched.

### Execution Output

```text
KIND         NAME        ACTION    FIELDS
agent        researcher  delete
environment  default     retained
```

### Usage Examples

```bash
# Example 1: delete declared Agents
astrabox destroy --file astrabox.yaml --yes

# Example 2: shorthand
astrabox destroy -f astrabox.yaml --yes

# Example 3: delete declared Agents from a remote deployment
astrabox destroy -f astrabox.yaml --yes \
  --endpoint https://astrabox.example.com

# Example 4: return machine-readable results
astrabox destroy -f astrabox.yaml --yes --output json
```

### Important Notes

1. **Deletion cannot be inferred from apply**: Removing an Agent from `astrabox.yaml` does not delete it. Use `destroy` with a file that declares the Agent.
2. **The whole Agent declaration is not required for matching**: The command matches declared Agents by `name`; the file must still satisfy the document and resource schemas.
3. **Ambiguous names fail**: If several stored Agents share the declared name, the command exits with a conflict instead of choosing one.
4. **Environment deletion is not supported**: Environments remain on the deployment.
5. **Keep a recoverable declaration**: Store the reviewed file in version control if you may need to recreate the Agent later.

---

## Common Options

### --help View Help

All commands support the `--help` option:

```bash
# View help for a specific command
astrabox apply --help
astrabox run --help

# View the list of all commands
astrabox --help
```

### Connection options

Commands that talk to a deployment accept the same connection options:

| Option | Description |
| :--- | :--- |
| `--endpoint` | Deployment base URL. |
| `--token` | Bearer token for this invocation. |
| `--output`, `-o` | `table` for people or `json` for scripts. |

The endpoint is resolved in this order:

1. `--endpoint`
2. `ASTRABOX_ENDPOINT`
3. `http://127.0.0.1:$ASTRABOX_SERVER_HOST_PORT`, with port `8088` when the variable is unset

A server started directly with `astrabox serve` uses port `8000` by default, so set `--endpoint http://127.0.0.1:8000` for that process.

### Authentication {#authentication}

A deployment in the default local identity mode accepts requests without a credential. When authentication is enabled, the CLI resolves credentials in this order:

1. `--token`
2. `ASTRABOX_TOKEN`
3. OAuth client credentials

For OAuth client credentials, set all three required variables:

```bash
export ASTRABOX_CLIENT_ID=<client-id>
export ASTRABOX_CLIENT_SECRET=<client-secret>
export ASTRABOX_TOKEN_URL=https://identity.example.com/oauth/token
export ASTRABOX_SCOPE=astrabox:admin  # Optional
astrabox get agents
```

A partial OAuth configuration fails with exit code 2 and names the missing variables. `ASTRABOX_SCOPE` is sent only when set.

`apply`, `diff`, and `destroy` use administration APIs, so an OAuth client for those commands needs the `astrabox:admin` scope. See [API authentication](../api-authentication.md) for the scope and token rules.

### Output formats and exit codes

Resource, Agent, and status commands accept human-readable default output and machine-readable JSON output; the default rendering depends on the command. Under `--output json`, failures are JSON as well as successes.

`astrabox logs` is the exception: logs are the payload, so it prints raw Compose logs in either output mode. `astrabox mcp serve` writes MCP protocol messages to stdout.

| Code | Meaning |
| :--- | :--- |
| `0` | Success. |
| `1` | The deployment rejected the request, a resource is absent, or an Agent run reported an error. |
| `2` | Invalid command usage or an unreadable/invalid document. |
| `3` | Authentication or authorization failed. |
| `4` | The endpoint, token endpoint, Session stream, or readiness deadline could not be reached. |
| `5` | The command cannot proceed without guessing, such as an ambiguous name or stale Agent version. |

When an API request fails, JSON output retains the deployment's registered error code in the `code` field.

---

## Platform Service Commands

In addition to resource commands, AstraBox CLI provides local deployment, MCP integration, and operator commands.

### `astrabox up`

Starts the maintained Compose deployment from an AstraBox source checkout and, by default, waits until it is ready.

```bash
# Start the deployment
astrabox up

# Rebuild service images before starting
astrabox up --build

# Change the readiness deadline
astrabox up --wait-seconds 600

# Return after Compose starts the services
astrabox up --no-wait

# Return machine-readable startup information
astrabox up --output json
```

The preflight requires an AstraBox checkout, a working Docker CLI and daemon, and the maintained Compose files. The result reports the repository, Docker server version, endpoint, readiness result, and Agent sandbox image status. If the default Agent sandbox image is missing, build it with `make build-agent-image`. Set `ASTRABOX_AGENT_IMAGE` when the deployment uses a different Agent sandbox image.

### `astrabox down`

Stops the maintained Compose deployment from an AstraBox source checkout.

```bash
# Stop services and keep their volumes
astrabox down

# Stop services and delete the database and all named-volume state
astrabox down --volumes
```

`--volumes` is destructive. Without it, the named volumes remain for the next startup.

### `astrabox logs`

Reads logs from the maintained Compose deployment.

```bash
# Show the last 200 lines per service
astrabox logs

# Show one service
astrabox logs server

# Change the line count
astrabox logs server --tail 100

# Follow all services
astrabox logs --follow
```

The optional service names a Compose service, such as `server`, `postgres`, `redis`, `sandbox-edge`, or `sandbox-dns-edge`. Logs remain raw text even when `--output json` is present.

### `astrabox mcp serve`

Serves AstraBox administration operations as MCP tools over stdio.

```bash
astrabox mcp serve

astrabox mcp serve \
  --endpoint https://astrabox.example.com
```

The server uses line-delimited JSON-RPC on stdin/stdout and exposes these tools:

| Tool | Operation |
| :--- | :--- |
| `astrabox_schema` | Read an Agent or Environment authoring schema. |
| `astrabox_get` | List one resource collection. |
| `astrabox_export` | Export Environments and Agents as an `astrabox.yaml` document body. |
| `astrabox_diff` | Preview a document without writing. |
| `astrabox_apply` | Create or update resources declared by a document. |
| `astrabox_status` | Probe deployment health and readiness. |
| `astrabox_run` | Start or continue a conversation and send one task. |

Register it as a stdio MCP Server in the client. This command administers an AstraBox deployment; the deployment's own `/api/v1/mcp` endpoint lets clients [use the Agents already exposed through MCP](../agent-mcp.md).

### Operator commands

#### `astrabox serve`

Starts the AstraBox FastAPI application with Uvicorn. This is a direct server process, not the maintained Compose deployment.

```bash
astrabox serve
astrabox serve --host 0.0.0.0 --port 8000
astrabox serve --reload --log-level debug
```

| Option | Description | Default |
| :--- | :--- | :--- |
| `--host` | Bind address. | `ASTRABOX_HOST`, then `127.0.0.1` |
| `--port` | Bind port. | `ASTRABOX_PORT`, then `8000` |
| `--reload` | Reload on code changes; development only. | Disabled |
| `--log-level` | Uvicorn log level. | `ASTRABOX_LOG_LEVEL`, then `info` |

When the bind address is not loopback and `ASTRABOX_WEB_IDENTITY` is `none`, the command refuses to start without authentication. Configure a supported identity mode, or explicitly set `ASTRABOX_ALLOW_UNAUTHENTICATED_BIND=1` only when the surrounding network already prevents unauthorized access to the deployment.

#### `astrabox verify-opensandbox-snapshots`

Creates a real sandbox, writes a marker, pauses it, resumes the same sandbox, reads the marker, and removes the test sandbox.

```bash
astrabox verify-opensandbox-snapshots

astrabox verify-opensandbox-snapshots \
  --image <agent-image> \
  --lifecycle-base-url http://127.0.0.1:8080 \
  --timeout-seconds 720 \
  --json-out snapshot-evidence.json
```

This operator command contacts a real OpenSandbox lifecycle service. `--image` uses `ASTRABOX_AGENT_IMAGE` when omitted, the timeout must be between 1 and 720 seconds, and `--json-out` writes non-secret evidence atomically.

---

## Common Workflows

### 📝 Complete Local Deployment Workflow

Suitable for starting AstraBox from a source checkout and then managing it through a file:

```bash
# 1️⃣ Install the package
make install

# 2️⃣ Build the Agent sandbox image
make build-agent-image

# 3️⃣ Start the maintained deployment
.venv/bin/astrabox up

# 4️⃣ Check readiness
.venv/bin/astrabox status

# 5️⃣ Create or export configuration
.venv/bin/astrabox init --from-deployment

# 6️⃣ Preview and apply changes
.venv/bin/astrabox diff -f astrabox.yaml
.venv/bin/astrabox apply -f astrabox.yaml

# 7️⃣ Send an Agent a task
.venv/bin/astrabox run <agent-name> "Check the deployment"
```

Connect a model service and create an Agent through the web console or `astrabox.yaml` before the final command.

### 🔄 Administer an Existing Remote Deployment

Suitable when AstraBox is already running on another host:

```bash
# 1️⃣ Set the deployment address and credential
export ASTRABOX_ENDPOINT=https://astrabox.example.com
export ASTRABOX_TOKEN=<access-token>

# 2️⃣ Check the deployment
astrabox status

# 3️⃣ Export its current configuration
astrabox init --from-deployment --file remote.astrabox.yaml

# 4️⃣ Review an edit
astrabox diff -f remote.astrabox.yaml

# 5️⃣ Apply it
astrabox apply -f remote.astrabox.yaml

# 6️⃣ Use an Agent
astrabox run <agent-name> "Report current repository status"
```

### 🔄 Quick Iteration Workflow

After editing `astrabox.yaml`:

```bash
# Method 1: preview, then apply
astrabox diff -f astrabox.yaml
astrabox apply -f astrabox.yaml

# Method 2: use apply's dry-run mode, then apply
astrabox apply -f astrabox.yaml --dry-run
astrabox apply -f astrabox.yaml
```

When another user has edited the same Agent, a stale-version conflict stops the update. Re-run `diff`, review the current state, and apply again.

### 🌍 Multiple Deployment Management

Use a separate file and endpoint for each deployment:

```bash
# Development deployment
astrabox diff -f development.astrabox.yaml \
  --endpoint https://dev.astrabox.example.com
astrabox apply -f development.astrabox.yaml \
  --endpoint https://dev.astrabox.example.com

# Production deployment
astrabox diff -f production.astrabox.yaml \
  --endpoint https://astrabox.example.com
astrabox apply -f production.astrabox.yaml \
  --endpoint https://astrabox.example.com
```

The file contains no target address, so the same declaration can also be checked against more than one deployment when that is the intended workflow.

---

## FAQ

### ❌ Configuration file not found

**Error message:**

```
astrabox.yaml: file does not exist
```

**Solution:**

```bash
# For new configuration
astrabox init

# For a deployment that already has Environments and Agents
astrabox init --from-deployment

# Or specify the existing file
astrabox diff --file path/to/astrabox.yaml
```

### ❌ Docker not running (Local Deployment)

**Error message:**

```
astrabox: Docker is installed but its daemon is not answering
```

**Solution:**

1. Start Docker Desktop or the Docker Engine service.
2. Confirm that `docker info` succeeds.
3. Run `astrabox up` again from an AstraBox source checkout.

The `up`, `down`, and `logs` commands require the source checkout because the maintained Compose definition lives there.

### ❌ Authentication not configured (Remote Deployment)

**Error message:**

```
astrabox: deployment refused the request with HTTP 401
```

**Solution:**

```bash
# Use a bearer token
export ASTRABOX_TOKEN=<access-token>

# Or configure OAuth client credentials
export ASTRABOX_CLIENT_ID=<client-id>
export ASTRABOX_CLIENT_SECRET=<client-secret>
export ASTRABOX_TOKEN_URL=https://identity.example.com/oauth/token
export ASTRABOX_SCOPE=astrabox:admin  # Optional
```

A partially configured OAuth client credential exits with code 2 and names every missing variable.

### ❌ Apply failed

Use the exit code and JSON error instead of matching message text:

```bash
astrabox apply -f astrabox.yaml --output json
```

Common causes:

1. The file is invalid YAML or has an unsupported document version.
2. A resource contains a key the deployment's authoring schema does not accept.
3. An Environment omits a field required for its complete replacement.
4. Several Agents share one declared name, so matching is ambiguous.
5. An Agent changed after the CLI read it, so optimistic concurrency refused the stale update.

Run `astrabox diff -f astrabox.yaml` again after correcting the file or reviewing the latest resource state.

### 💡 Debugging Tips

```bash
# Check command syntax
astrabox apply --help

# Check whether the deployment is reachable and ready
astrabox status --output json

# Inspect the accepted resource fields
astrabox schema agent --output json
astrabox schema environment --output json

# Inspect complete stored documents
astrabox get agents --output json
astrabox get environments --output json

# Read local service logs
astrabox logs server --tail 200
```

For local startup failures, `astrabox up` reports missing checkout files, Docker availability, readiness failure, and Agent sandbox image status separately.

### What the CLI does not manage

- **Trigger bindings**: `astrabox get deployments` is not available and `apply` does not configure channel or schedule bindings. Configure them in the Web console; see [Deployments](../deployments.md).
- **Answers to pending questions**: `run` reports when an Agent is waiting for an answer but cannot submit that answer. Use the Web console or [HTTP API](../api.md).
- **Non-Compose deployment lifecycle**: `up`, `down`, and `logs` operate the maintained Compose deployment. Follow [Deploy AstraBox](../deploy.md) for Kubernetes or an existing OpenSandbox installation; client commands such as `status`, `get`, and `apply` work after that deployment is running.

---

## Next Steps

- 📖 [Configuration Guide](./configuration.md) - Deep dive into the file structure and resource fields
- 🚀 [Quick Start](../quickstart.md) - Start AstraBox and create your first Agent
- 🛠️ [Deploy AstraBox](../deploy.md) - Configure and operate a self-hosted deployment
