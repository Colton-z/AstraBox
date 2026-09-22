---
title: Configuration File Reference
---

# Configuration File Reference

`astrabox.yaml` is the core configuration file for an AstraBox deployment. It declares the Environments and Agents you want the deployment to provide.

## Configuration system overview

AstraBox uses one declarative file with two resource types:

| Resource | Purpose |
| :--- | :--- |
| **Environment** | Selects the Agent program, model-service connection, sandbox behavior, credentials, and runtime defaults that Agents can share. |
| **Agent** | Defines the model, system prompt, MCP Servers, Skills, Plugins, repository, visibility mode, and other settings for one Agent. |

The file is applied to the deployment named by `--endpoint` or `ASTRABOX_ENDPOINT`. It does not contain the deployment address or CLI credential.

Use `astrabox diff -f astrabox.yaml` to preview the result and `astrabox apply -f astrabox.yaml` to create or update the declared resources.

## File structure

The configuration file has three top-level keys:

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

**The three sections**:

- **version** - configuration document version; the current and only accepted value is `1`
- **environments** - optional list of Environment declarations
- **agents** - optional list of Agent declarations

---

## Document root

The document root defines the configuration version and the two resource lists.

### Example

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

### Fields

#### `version` (required)

**The configuration document version**

- 📝 **What it does**: selects the top-level file format read by the CLI
- ✅ **Rules**: must be the integer `1`
- 🎯 **Used for**: refusing a file whose structure the installed CLI does not understand

**Example**:

```yaml
version: 1
```

This is the `astrabox.yaml` document version. It is not the AstraBox release number and not an Agent's optimistic-concurrency version.

#### `environments` (optional)

**Environment declarations**

- 📝 **What it does**: lists the reusable runtime and model-service settings that should exist
- ✅ **Rules**: must be a list of mappings; every item needs a non-empty `name`
- ✅ **Default**: an empty list when omitted
- 🎯 **Used for**: creating or updating Environments before Agents are processed

**Example**:

```yaml
environments:
  - name: default
    engine_kind: <agent-program-id>
    endpoint_provider: <model-connection-id>
    enabled: true
```

Each Environment is a complete replacement when written. Include all required fields for that Environment, preferably by starting from `astrabox init --from-deployment`.

#### `agents` (optional)

**Agent declarations**

- 📝 **What it does**: lists the Agents that should be created or updated
- ✅ **Rules**: must be a list of mappings; every item needs a non-empty `name`
- ✅ **Default**: an empty list when omitted
- 🎯 **Used for**: configuring what each Agent uses, can access, and exposes

**Example**:

```yaml
agents:
  - name: researcher
    model: <model-name>
    environment_name: default
    system: |
      Research the requested topic and cite the sources you use.
    enabled: true
```

Agent updates include only the declared fields. The CLI sends the stored Agent version with an update so a concurrent edit is refused instead of overwritten.

#### `name` (required for every resource)

**The resource name**

- 📝 **What it does**: identifies a declaration before the deployment has assigned an ID
- ✅ **Rules**: must contain a non-empty value and may appear only once within the same resource list
- 🎯 **Used for**:
  - matching an existing Environment
  - matching an existing Agent
  - reporting each `diff`, `apply`, or `destroy` action

**Examples**:

```yaml
environments:
  - name: default

agents:
  - name: researcher
```

A deployment may contain several Agents with the same name. When that makes one declaration ambiguous, the CLI exits with code 5 and lists the matching Agent IDs instead of choosing one.

#### Unknown top-level keys

The root is closed to these three keys: `version`, `environments`, and `agents`. A misspelled or extra top-level key fails before any request is sent.

```yaml
version: 1
agent: []  # Invalid: the accepted key is agents
```

Resource fields are also validated, but their accepted keys come from the Agent and Environment definitions described below.

---

## Environment configuration

An Environment is a reusable set of Agent-program, model-service, sandbox, networking, credential, and tracing settings. Agents select it by `environment_name`.

### Example

```yaml
environments:
  - name: default
    display_name: Default
    description: General-purpose Agent environment
    engine_kind: <agent-program-id>
    endpoint_provider: <model-connection-id>
    provider_access:
      base_url: https://models.example.com
      api_key_secret_name: model-api-key
    networking:
      type: limited
      allowed_hosts:
        - models.example.com
      allow_mcp_servers: true
    enabled: true
```

Replace the bracketed values with values supported by the target deployment.

### Fields

#### `engine_kind`

Selects the Agent program that runs inside the sandbox.

- **Required**: Yes
- **Candidate values**: Agent programs installed in the target deployment
- **Inspect with**: `astrabox schema environment`

Do not copy a value from another deployment without checking it; Plugins can add Agent programs to one deployment without adding them to another.

#### `endpoint_provider`

Selects the model-service connection type used by this Environment.

- **Required**: No
- **Candidate values**: model connections registered in the target deployment
- **Used with**: `provider_access`

#### `sandbox_backend`

Selects the sandbox backend that creates Agent sandboxes.

- **Required**: No
- **Candidate values**: sandbox backends installed in the target deployment

`runtime_template_name` selects a backend runtime template.

#### `provider_access`

Configures how Agents on this Environment reach the model service:

```yaml
provider_access:
  base_url: https://models.example.com
  api_key_secret_name: model-api-key
```

| Field | Description |
| :--- | :--- |
| `base_url` | Model-service or gateway base URL. |
| `api_key` | Inline model credential; returned as a mask in read views. |
| `api_key_secret_name` | Logical name of a credential in the AstraBox service's process environment. |

Use either an inline key or an environment secret name as supported by the selected model connection. Logical names are uppercased and hyphens become underscores: `model-api-key` reads `MODEL_API_KEY`. This lookup does not read a Web-console Vault. Do not commit plaintext credentials to version control.

#### `networking`

Controls outbound networking for Agent sandboxes:

```yaml
networking:
  type: limited
  allowed_hosts:
    - models.example.com
  allow_mcp_servers: true
```

| Field | Description |
| :--- | :--- |
| `type` | Network mode supported by the deployment. |
| `allowed_hosts` | Additional hostnames reachable in limited mode. |
| `allow_mcp_servers` | Allow destinations required by the Agent's configured MCP Servers. |

#### `idle_action`

Controls what happens to an idle sandbox. The deployment accepts only actions supported by the selected sandbox backend.

#### `sandbox_tenancy` / `sandbox_permission_level`

`sandbox_tenancy` selects whether a sandbox belongs to one conversation or can serve the Agent more broadly. `sandbox_permission_level` selects the permission level granted inside the sandbox.

Agent-level tenancy requires a permission level that can enforce its isolation model. Unsupported combinations fail when the Environment is written.

#### `tracing` {#tracing}

Configures traces emitted by the Agent program:

```yaml
tracing:
  enabled: true
  endpoint: https://otel.example.com
  environment: production
  signals:
    - traces
```

The block declares `endpoint`, `headers`, inline authentication, an environment label, signal selection, and whether user prompts may be logged. Tracing is refused when the selected Agent program cannot emit it. The example assumes a collector that does not require authentication.

For an authenticated collector, supply either `auth_token` or `auth_token_secret_name`. Both use the complete `Authorization` value, such as `Bearer your-token` or `Basic base64-value`. A secret name resolves from the AstraBox server's environment: `otel-auth` reads `OTEL_AUTH` (uppercase, with hyphens replaced by underscores). It does not refer to a Web-console Vault entry.

Tracing is consumed by Claude Code. Its resolved credential is passed to the CLI through `OTEL_EXPORTER_OTLP_HEADERS`, without egress Vault substitution; use HTTPS to protect it in transit. Disabled tracing does not read the secret. If an enabled reference is missing or empty, AstraBox logs the Environment and reference name and disables tracing for that runtime configuration without failing the conversation or exporting anonymously. The stored Environment remains unchanged.

#### Display and state fields

| Field | Description |
| :--- | :--- |
| `name` | Stable name used by Agent `environment_name`; required. |
| `display_name` | Name shown to users. |
| `description` | What the Environment is intended for. |
| `enabled` | Whether the Environment can be selected for use. |

### Auto-managed fields

The deployment fills normalized defaults such as the idle action and networking shape when they are omitted. `astrabox init --from-deployment` exports the settled values that the deployment currently stores.

IDs, timestamps, ownership, and other server-managed state are excluded from `astrabox.yaml`.

---

## Agent configuration

An Agent combines a model, system prompt, MCP Servers, Skills, Plugins, repository settings, and an Environment into a cloud Agent that is available whenever the AstraBox deployment is running.

### Example

```yaml
agents:
  - name: researcher
    display_name: Researcher
    description: Researches a topic and cites its sources
    model: <model-name>
    system: |
      Research the requested topic and cite the sources you use.
    environment_name: default
    skills:
      - <skill-reference>
    mcp_servers: {}
    default_repo:
      url: git@example.com:team/research.git
      protocol: ssh
      branch: main
    exposure_mode: chat_only
    enabled: true
```

Replace the bracketed values and remove optional fields you do not need.

### Literal values

`astrabox.yaml` is parsed as YAML. The CLI does not render shell variables or template expressions inside the file.

```yaml
model: <model-name>       # Documentation placeholder: replace it
model: ${MODEL_NAME}      # Literal text, not environment-variable expansion
```

Set the deployment address and CLI credential through command flags or environment variables. Keep runtime credentials in the Environment's `provider_access` or AstraBox Vault instead of interpolating them into the file.

### No `Auto` keyword

AstraBox does not use an `Auto` keyword in `astrabox.yaml`. Omit an optional field when the deployment should choose its default. Required fields must carry an explicit value accepted by the deployment.

### Fields

#### `name`

Required stable name used by `astrabox.yaml` to match an existing Agent. Server-generated `agent_id` and `version` are not configuration fields.

#### `model`

Required model or model-route name used by the selected Environment. It is a free-text value because a self-hosted model gateway can provide names that AstraBox cannot enumerate globally.

```yaml
model: <model-name>
```

#### `system`

Optional system prompt supplied to the Agent program:

```yaml
system: |
  Review the repository carefully.
  Explain the evidence for each conclusion.
```

#### `environment_name`

Required name of an existing Environment. Environments in the same file are applied first, so one document can create an Environment and then create Agents that use it.

#### `engine_options`

Optional settings defined by the selected Agent program. AstraBox carries these settings to that program without translating them into a second vocabulary.

```yaml
engine_options:
  <engine-declared-json-block>:
    <native-option>: <value>
```

Use the JSON blocks declared by the Environment's engine. Their contents follow
the installed supplier's native configuration, not a platform field whitelist.
See [Native runtime JSON](../authoring-agents.md#native-runtime-json).

#### `skills`

Optional list of Skills available to the Agent:

```yaml
skills:
  - <skill-reference>
```

A simple Agent can omit the field. The list is combined with Skills supplied by installed Plugins.

#### `mcp_servers`

Optional name-keyed MCP Server definitions available to the Agent:

```yaml
mcp_servers:
  source-control:
    <server-setting>: <value>
```

The setting shape follows the selected Agent program's MCP support. Remote MCP Servers assigned through the AstraBox registry are combined with the Agent's own definitions.

#### `default_repo` / `plugin_repos`

`default_repo` checks out the Agent's main repository. `plugin_repos` adds repositories that provide Plugins.

```yaml
default_repo:
  url: git@example.com:team/application.git
  protocol: ssh
  deploy_key_secret_name: application-deploy-key
  branch: main
  depth: 1

plugin_repos:
  - url: https://github.com/example/agent-plugins.git
    protocol: https
    branch: main
    plugin_paths:
      - plugins/review
```

Repository objects support `url`, `protocol`, a deploy-key secret name when required by the protocol, branch, and depth. Deploy-key names resolve from the AstraBox service's process environment, not a Web-console Vault. Plugin repositories may also pin a commit with `sha` and select `plugin_paths`.

#### `exposure_mode`

Controls how other applications can use the Agent:

| Value | Result |
| :--- | :--- |
| `chat_only` | Available for conversations. |
| `mcp_only` | Available through the deployment's Agent MCP endpoint. |
| `both` | Available through both surfaces. |

#### `idle_hibernate_seconds` / `prewarm_enabled`

`idle_hibernate_seconds` controls how long an idle Agent waits before its sandbox is hibernated. `prewarm_enabled` asks AstraBox to keep a complete runtime ready for the Agent; the selected sandbox deployment must support prepared capacity.

#### Display and state fields

| Field | Description |
| :--- | :--- |
| `display_name` | Name shown to users. |
| `description` | What the Agent is intended to do. |
| `icon` | Icon reference shown with the Agent. |
| `tags` | Labels for organizing Agents. |
| `use_cases` | Example tasks presented to users. |
| `enabled` | Whether the Agent can be used. |

### Auto-managed fields

The deployment assigns `agent_id`, timestamps, ownership, and optimistic-concurrency `version`. They are excluded from exported configuration and must not be added to `astrabox.yaml`.

---

## Applying both resource types

One file can declare Environments and the Agents that use them. The CLI applies the dependency in the correct order.

### Example

```yaml
version: 1

environments:
  - name: research
    engine_kind: <agent-program-id>
    endpoint_provider: <model-connection-id>
    enabled: true

agents:
  - name: researcher
    model: <model-name>
    environment_name: research
    system: |
      Research the requested topic and cite the sources you use.
    enabled: true
```

### How the two resource types differ

| Behavior | Environment | Agent |
| :--- | :--- | :--- |
| Identity in the file | `name` | `name` |
| Apply order | First | After Environments |
| Create or update | One PUT upserts by name | Create or update after matching by name |
| Update body | Complete replacement | Declared fields, plus stored version |
| Concurrent edit | Latest complete document is written | Stale version is refused |
| Destroy | Retained; no delete route | Deleted when declared and `--yes` is present |

### Fields

#### Apply order

Environments are always processed before Agents, regardless of their visual position within their separate YAML lists. An Agent's `environment_name` can therefore refer to an Environment created by the same apply.

#### Resource matching

IDs are created by the deployment and are not stored in `astrabox.yaml`. The CLI matches resources by `name`.

Environment names are the resource key. Agent names are not required to be globally unique by the API, so several matching Agents produce a conflict instead of an arbitrary choice.

`astrabox init --from-deployment` applies the same rule before writing: if the visible Agents contain a duplicate name, it exits with conflict code `5`, reports every matching Agent ID, and leaves the destination file untouched. Rename or remove the ambiguous Agents, then export again.

#### No implicit deletion

`astrabox apply` creates and updates declarations but never deletes resources that disappeared from the file.

```bash
# Preview and apply creates or updates
astrabox diff -f astrabox.yaml
astrabox apply -f astrabox.yaml

# Explicitly delete Agents declared by this file
astrabox destroy -f astrabox.yaml --yes
```

### Auto-managed fields

When an Agent is updated, the CLI reads and sends its stored `version`. When a resource is created, the deployment assigns its ID, ownership, timestamps, and other server-managed state.

---

## Resource field definitions

The three top-level keys in `astrabox.yaml` are fixed. Fields inside an Environment or Agent follow the authoring definitions of the deployment that receives the file.

### Example

```bash
# Human-readable tables
astrabox schema environment
astrabox schema agent

# Complete descriptors for scripts and nested objects
astrabox schema environment --output json
astrabox schema agent --output json
```

A table row looks like this:

```text
KEY               TYPE      REQUIRED  ENUM
name              string    true
engine_kind       enum      true      <installed Agent programs>
endpoint_provider enum      false     <installed model connections>
```

### Fields

#### `key`

The YAML field name to place in an Environment or Agent declaration.

#### `type`

The value shape expected by the deployment. Common types include strings, text, integers, booleans, enums, string lists, objects, object lists, and Environment references.

#### `required`

Whether the field must be present when that resource is written. Because Environment writes are complete replacements, all required Environment fields need to remain in the declaration.

#### `enum`

Candidate values for a fixed choice. Agent-program, sandbox-backend, permission, and model-connection candidates can differ between deployments because installed Plugins extend the registries.

#### `item_schema`

Field definitions for a structured object or list item. It describes nested values such as Environment networking, model access, tracing, and repository settings.

#### `path`

Where a writable field is stored in the resource document when it differs from its YAML key. The CLI follows this path during `diff`, so display fields nested by the server do not appear changed on every run.

#### `default`

The value the deployment supplies when the field is omitted. A default belongs to the target deployment; the CLI does not add its own resource defaults.

### Open Agent-program settings

`engine_options` and the contents of an Agent's own `mcp_servers` map follow the selected Agent program's settings format. AstraBox carries those settings without inventing parallel names for vendor-defined behavior.

---

## Deployment connection configuration

The target deployment and CLI credential do not belong in `astrabox.yaml`. Supply them through flags or process environment variables so one declaration is not tied to an address or access token.

### Location

There is no AstraBox user-level configuration file. Connection settings come from the current command and its process environment.

### Example

```bash
# Bearer token
export ASTRABOX_ENDPOINT=https://astrabox.example.com
export ASTRABOX_TOKEN=<access-token>
astrabox diff -f astrabox.yaml

# OAuth client credentials
export ASTRABOX_ENDPOINT=https://astrabox.example.com
export ASTRABOX_CLIENT_ID=<client-id>
export ASTRABOX_CLIENT_SECRET=<client-secret>
export ASTRABOX_TOKEN_URL=https://identity.example.com/oauth/token
export ASTRABOX_SCOPE=astrabox:admin  # Optional
astrabox apply -f astrabox.yaml
```

### Precedence

**Deployment address**:

```
--endpoint > ASTRABOX_ENDPOINT > maintained local address
```

**Credential**:

```
--token > ASTRABOX_TOKEN > OAuth client credentials > unauthenticated request
```

OAuth client credentials require `ASTRABOX_CLIENT_ID`, `ASTRABOX_CLIENT_SECRET`, and `ASTRABOX_TOKEN_URL` together. `ASTRABOX_SCOPE` is optional.

### Typical use cases

**Use one file against a named deployment**:

```bash
ASTRABOX_ENDPOINT=https://dev.astrabox.example.com \
  astrabox diff -f astrabox.yaml
```

**Override the address for one command**:

```bash
astrabox apply -f astrabox.yaml \
  --endpoint https://astrabox.example.com
```

Do not put CLI access tokens, OAuth client secrets, or the target address inside `astrabox.yaml`.

---

## Best practices

### 🌍 Multiple deployment management

Use separate files when deployments intentionally differ:

```
config/
├── development.astrabox.yaml
├── staging.astrabox.yaml
└── production.astrabox.yaml
```

```bash
# Development
astrabox diff -f config/development.astrabox.yaml \
  --endpoint https://dev.astrabox.example.com

# Production
astrabox diff -f config/production.astrabox.yaml \
  --endpoint https://astrabox.example.com
```

Keep the endpoint outside the file. This makes the target of a write explicit at command time.

### 🔐 Secure handling of secrets

**Do not commit plaintext credentials**:

```yaml
# ❌ Do not commit
provider_access:
  api_key: <plaintext-model-key>

# ✅ Reference a credential in the AstraBox service environment
provider_access:
  api_key_secret_name: production-model-key

# ✅ Reference a repository deploy key in the AstraBox service environment
default_repo:
  url: git@example.com:team/application.git
  protocol: ssh
  deploy_key_secret_name: application-deploy-key
```

For these examples, set `PRODUCTION_MODEL_KEY` and `APPLICATION_DEPLOY_KEY` on the AstraBox service through the deployment's protected secret configuration. Web-console Vault assignments are a separate credential mechanism. CLI authentication belongs in `ASTRABOX_TOKEN` or OAuth environment variables, not in `astrabox.yaml`.

When an Environment contains an inline credential, read views and `astrabox init --from-deployment` return a masked value. Applying that mask unchanged to the same Environment keeps its stored credential.

If a file contains plaintext secrets while you are preparing it locally, exclude it from version control:

```gitignore
# .gitignore
*.private.astrabox.yaml
```

Commit a secret-free declaration or template instead:

```yaml
# astrabox.yaml
provider_access:
  api_key_secret_name: production-model-key
```

### 📝 Add helpful comments

YAML comments can explain why a setting exists:

```yaml
environments:
  - name: restricted
    # Only the model gateway and configured remote MCP Servers are reachable.
    networking:
      type: limited
      allowed_hosts:
        - models.example.com
      allow_mcp_servers: true
```

`diff` and `apply` read but do not rewrite the file, so comments remain. `init --from-deployment` writes a new export and does not preserve comments from another file.

### ✅ Validate configuration regularly

```bash
# Option 1: inspect the accepted fields
astrabox schema environment
astrabox schema agent

# Option 2: parse, validate, and preview without writes
astrabox diff -f astrabox.yaml

# Option 3: return a machine-readable preview in CI
astrabox diff -f astrabox.yaml --output json
```

The CLI validates the complete document before sending any write, then the deployment validates each resource against the same field definitions as its Web forms.

---

## Full examples

### 📱 Local self-hosted configuration

```yaml
version: 1

environments:
  - name: local
    display_name: Local
    engine_kind: <agent-program-id>
    endpoint_provider: <model-connection-id>
    provider_access:
      base_url: http://host.docker.internal:4000
      api_key_secret_name: local-model-key
    networking:
      type: limited
      allowed_hosts:
        - host.docker.internal
      allow_mcp_servers: true
    enabled: true

agents:
  - name: developer
    display_name: Developer
    model: <model-name>
    environment_name: local
    system: |
      Help with the repository and explain each change.
    enabled: true
```

### Repository and extension configuration

```yaml
version: 1

agents:
  - name: reviewer
    display_name: Reviewer
    description: Reviews changes in the application repository
    model: <model-name>
    environment_name: default
    system: |
      Review the change for correctness, tests, and operational risk.
    default_repo:
      url: git@example.com:team/application.git
      protocol: ssh
      deploy_key_secret_name: application-deploy-key
      branch: main
      depth: 1
    plugin_repos:
      - url: https://github.com/example/review-plugins.git
        protocol: https
        branch: main
        plugin_paths:
          - plugins/review
    skills:
      - <skill-reference>
    mcp_servers: {}
    enabled: true
```

### Production configuration

```yaml
version: 1

environments:
  - name: production
    display_name: Production
    description: Restricted production Agent environment
    engine_kind: <agent-program-id>
    sandbox_backend: <sandbox-backend-id>
    endpoint_provider: <model-connection-id>
    provider_access:
      base_url: https://models.example.com
      api_key_secret_name: production-model-key
    networking:
      type: limited
      allowed_hosts:
        - models.example.com
      allow_mcp_servers: true
    tracing:
      enabled: true
      endpoint: https://otel.example.com
      environment: production
      signals:
        - traces
        - metrics
    enabled: true

agents:
  - name: incident-reviewer
    display_name: Incident reviewer
    description: Collects evidence and drafts incident reviews
    model: <model-name>
    environment_name: production
    system: |
      Collect evidence before drawing conclusions.
      Cite every log, change, and timeline item you use.
    exposure_mode: both
    prewarm_enabled: true
    enabled: true
```

### 🎯 Minimal configuration examples

**Minimal Environment**:

```yaml
version: 1
environments:
  - name: default
    engine_kind: <agent-program-id>
```

**Minimal Agent**:

```yaml
version: 1
agents:
  - name: assistant
    model: <model-name>
    environment_name: default
```

The Environment referenced by a minimal Agent must already exist when it is not declared in the same file.

---

## FAQ

### ❓ Configuration file not found

**Problem**: the command cannot read `astrabox.yaml`.

**Solution**:

```bash
# Create the annotated skeleton
astrabox init

# Or export an existing deployment
astrabox init --from-deployment

# Or name the actual file
astrabox diff --file config/production.astrabox.yaml
```

### ❓ Invalid YAML format

**Problem**: indentation, quoting, or list syntax is invalid.

**Solution**:

1. Use spaces, not tabs.
2. Keep every resource under a dash (`-`) in its list.
3. Quote values that YAML could interpret as another type.
4. Run `astrabox diff -f astrabox.yaml`; invalid YAML exits with code 2 before any request is sent.

### ❓ Missing required fields

**Problem**: a resource is missing a required field.

**Solution**:

```bash
# Read the target deployment's required fields
astrabox schema environment
astrabox schema agent

# Export complete current resources before editing
astrabox init --from-deployment --force
```

Every resource needs `name`. The current Agent definition also requires `model` and `environment_name`; the current Environment definition requires `engine_kind`. The target deployment's field definitions and candidate values remain authoritative.

### ❓ Placeholders or environment variables were not replaced

**Problem**: a value such as `<model-name>` or `${MODEL_NAME}` reached validation literally.

**Solution**:

Replace documentation placeholders before applying the file. AstraBox CLI does not interpolate environment variables or template expressions inside `astrabox.yaml`.

Use process environment variables only for the deployment address and CLI credential:

```bash
export ASTRABOX_ENDPOINT=https://astrabox.example.com
export ASTRABOX_TOKEN=<access-token>
astrabox diff -f astrabox.yaml
```

### ❓ Configuration changes did not take effect

Check the reported action first:

```bash
# 1) Preview the file
astrabox diff -f astrabox.yaml

# 2) Apply the file
astrabox apply -f astrabox.yaml

# 3) Read the stored resource
astrabox get agents <agent-name> --output json
astrabox get environments <environment-name> --output json
```

- `unchanged` means the declared values already match.
- A resource removed from the file is retained; `apply` never deletes.
- An Agent uses the Environment named by `environment_name`.
- An Agent-program option only has an effect when the selected Agent program supports it.
- A masked Environment secret sent back unchanged preserves the stored value.

### ❓ Field names are incompatible

The document root accepts only `version`, `environments`, and `agents`. A resource accepts only fields declared by the target deployment.

Do not rename a rejected field or add a compatibility key. Export the current shape or read it directly:

```bash
astrabox init --from-deployment --force
astrabox schema environment --output json
astrabox schema agent --output json
```

---

## Configuration field quick reference

### Document root

| Field | Required | Description |
| :--- | :--- | :--- |
| `version` | ✅ | Must be `1`. |
| `environments` | ❌ | List of Environment declarations. |
| `agents` | ❌ | List of Agent declarations. |

### Environment fields

| Field | Required | Description |
| :--- | :--- | :--- |
| `name` | ✅ | Environment name and file identity. |
| `display_name` | ❌ | User-facing name. |
| `description` | ❌ | Intended use. |
| `engine_kind` | ✅ | Agent program. |
| `enabled` | ❌ | Whether the Environment can be used. |
| `sandbox_backend` | ❌ | Sandbox backend. |
| `runtime_template_name` | ❌ | Backend runtime template. |
| `networking` | ❌ | Sandbox outbound network policy. |
| `idle_action` | ❌ | Action for an idle sandbox. |
| `sandbox_tenancy` | ❌ | Conversation or Agent sandbox tenancy. |
| `sandbox_permission_level` | ❌ | Permission level inside the sandbox. |
| `endpoint_provider` | ❌ | Model-service connection type. |
| `provider_access` | ❌ | Model-service address and credential reference. |
| `tracing` | ❌ | Agent-program trace export. |

### Agent fields

| Field | Required | Description |
| :--- | :--- | :--- |
| `name` | ✅ | Agent name and file identity. |
| `display_name` | ❌ | User-facing name. |
| `description` | ❌ | Intended use. |
| `icon` | ❌ | User-facing icon reference. |
| `tags` | ❌ | Organizational labels. |
| `use_cases` | ❌ | Example tasks. |
| `model` | ✅ | Model or model-route name. |
| `system` | ❌ | System prompt. |
| `engine_options` | ❌ | Settings defined by the Agent program. |
| `skills` | ❌ | Skills available to the Agent. |
| `mcp_servers` | ❌ | Agent-owned MCP Server definitions. |
| `terminal_panel` | ❌ | Boolean; show the terminal panel. This does not control permission to execute commands. |
| `diff_panel` | ❌ | Boolean; show the file-diff panel. This does not control permission to change files. |
| `default_repo` | ❌ | Main repository checkout. |
| `plugin_repos` | ❌ | Repositories that provide Plugins. |
| `environment_name` | ✅ | Environment used by the Agent. |
| `exposure_mode` | ❌ | Conversation/MCP exposure. |
| `idle_hibernate_seconds` | ❌ | Idle time before hibernation. |
| `prewarm_enabled` | ❌ | Keep a complete Agent runtime ready. |
| `enabled` | ❌ | Whether the Agent can be used. |

---

## Next steps

- 📖 [CLI Overview](./overview.md) - learn the main capabilities and concepts
- 🎮 [Commands](./commands.md) - learn how each command works
- 🚀 [Quick Start](../quickstart.md) - follow an end-to-end walkthrough
