# Connect a model service

The built-in model connection sends every model request through LiteLLM.
LiteLLM gives Agents a stable model name while keeping the upstream address,
credential, routing, budget, and request log on the server. A deployment can
also install another model connection. The following settings apply to the
LiteLLM connection included with AstraBox.

![How an Agent reaches a model service](./img/models-request-path.svg#inline)

## Optional titles and process summaries

Automatic conversation titles and execution-process summaries are optional.
To disable both without changing Agent conversations, set this in the deployment YAML
and restart AstraBox:

```yaml
astrabox:
  title_model:
    enabled: false
```

The environment equivalent is `ASTRABOX_TITLE_MODEL_ENABLED=false`. Existing saved
titles and summaries remain readable. New work uses a fixed outer “Process” heading
and inner “Tool calls” or “Reasoning” headings; tool records remain expandable.
Generation is enabled by default. Empty model connection fields reuse the main
model configuration and do not disable generation.

## Choose the connection that fits your deployment

| Setup | Choose it when | What you configure |
|---|---|---|
| Bundled LiteLLM | You are installing AstraBox on one host or evaluating it | A model-service credential and a LiteLLM route |
| Existing LiteLLM | Your organization already operates a model gateway | Its sandbox-facing URL, a scoped inference key, and optionally a separate server-side URL |
| Local model through LiteLLM | The model runs on your workstation or private network | A LiteLLM route whose target is reachable from the AstraBox host |

In every LiteLLM setup, an Agent selects a **route name** such as
`company-code-model`. LiteLLM maps that name to the actual model service. The
route can move to another upstream model without changing every Agent that uses
it.

## Understand one model request

1. The Agent program sends the request in the protocol it supports.
2. The sandbox reaches the LiteLLM address configured for this AstraBox
   deployment.
3. LiteLLM looks up the Agent's route name, adds the upstream credential, and
   calls the model service.
4. LiteLLM streams the response back and records the configured usage data.

The Agent program receives the model name and gateway access needed for the
request. Upstream model-service keys stay with LiteLLM.

## Use the bundled gateway

The standard AstraBox container starts LiteLLM alongside the AstraBox service.
Start with a credential and model for a route in the bundled configuration:

```bash
export ANTHROPIC_API_KEY="your-anthropic-api-key"
export ANTHROPIC_MODEL="your-model-name"
scripts/compose.sh up --build -d
```

Open **Management console → Integrated services → LiteLLM gateway** to manage
model routes, provider credentials, keys, budgets, spend, and request logs. The
bundled gateway opens through the AstraBox administrator session instead of
requiring a separate LiteLLM login.

For configuration as code, the bundled routes are defined in
`containers/litellm/config.yaml`. Mount a replacement file at
`/opt/astrabox/litellm/config.yaml` in the AstraBox container to supply your own
routes:

```yaml
model_list:
  - model_name: "company-code-model"
    litellm_params:
      model: "openai/provider-model-id"
      api_base: "https://models.example.com/v1"
      api_key: os.environ/COMPANY_MODEL_API_KEY
```

Set `COMPANY_MODEL_API_KEY` on the AstraBox service, restart the deployment, and
use `company-code-model` in the Agent form. An explicit upstream model version
keeps model changes under the route administrator's control.

## Connect an existing LiteLLM gateway

Set the URL that sandboxes can reach and a key limited to inference routes:

```bash
ASTRABOX_LITELLM_BASE_URL=https://llm-gateway.example.com
ASTRABOX_LITELLM_API_KEY=sk-scoped-inference-key
```

`ASTRABOX_LITELLM_BASE_URL` is the address used for Agent model requests. Its
hostname has to resolve and be reachable from the sandbox network. Setting this
address disables the bundled LiteLLM process.

Some deployments expose the gateway to sandboxes through private DNS while the
AstraBox service reaches it through another service-network address. Set the
second address for model search in the Agent editor:

```bash
ASTRABOX_LITELLM_SERVER_BASE_URL=http://litellm.internal
```

To show the external gateway's browser console under **Integrated services**,
set its public administration URL:

```bash
ASTRABOX_LITELLM_ADMIN_URL=https://llm-admin.example.com/ui/
```

The team deployment overlay at `containers/compose.team-gateway.yaml` requires
the sandbox-facing gateway to use an HTTPS fully qualified domain on port 443
and requires an explicit scoped inference key.

## Connect a local model

Expose the local model through LiteLLM, then give it a stable route. For an
Ollama service reachable from the AstraBox host:

```yaml
model_list:
  - model_name: "local-code-model"
    litellm_params:
      model: "ollama_chat/qwen3:32b"
      api_base: "http://host.docker.internal:11434"
```

The configured address is resolved from the network where LiteLLM runs. On a
Linux or remote Docker host, replace `host.docker.internal` with an address that
the AstraBox container can resolve. See [Deploy AstraBox](deploy.md).

Choose a model and LiteLLM route compatible with the request protocol and tool
calling required by the selected Agent program.

## Choose the model on an Agent

Open **Management console → Agents** and create or edit an Agent. Select an
**Environment** first. The Environment determines which Agent program runs and
which sandbox settings, network access, and model connection it uses. The model
field then searches that Environment's model connection; select a result or
enter the exact route name.

The four parts have separate responsibilities:

| Part | Responsibility |
|---|---|
| Agent | Stores the selected model or route name |
| Environment | Selects the Agent program and model connection used when AstraBox prepares the runtime |
| Agent program | Formats the request in its native model protocol |
| LiteLLM | Maps the route to an upstream model and applies gateway routing, budgets, logging, and failover |

With the built-in LiteLLM connection, deployment settings choose the inference
gateway. Connection details stored on an Environment are also used when the
console queries that Environment's model list. If the gateway returns no model
list, the model field still accepts an exact route name.

Changing the Agent's model or its Environment does not reconfigure an active
runtime in place. AstraBox resolves the current settings the next time it first
prepares or rebuilds that Session runtime.

## Keep model credentials outside the Agent program

The bundled gateway holds upstream model-service keys. With protected
credential delivery enabled, the Agent program receives a placeholder gateway
credential, and the sandbox service adds the real value only to matching model
requests. The Agent stores no model key, and the Environment editor returns only
a mask for a saved value.

See [Protect credentials used by Agents](egress-credential-injection.md) for the
request-matching path.

## Configure conversation-title requests

Automatic titles use a separate, non-streaming model request from the AstraBox
server. Titles, title decisions and execution-process summaries explicitly send
`reasoning_effort: "none"`; configure a model endpoint that supports non-thinking
completions. LiteLLM translates this option for the selected provider. These
requests do not inherit an Agent's reasoning settings. Configure their timeout
in `astrabox/config/app.yml`:

```yaml
astrabox:
  title_model:
    request_timeout_seconds: 60
```

The default is 60 seconds. For a container deployment, set the equivalent value
in the Compose YAML's `services.server.environment`:

```yaml
services:
  server:
    environment:
      ASTRABOX_TITLE_MODEL_REQUEST_TIMEOUT_SECONDS: "60"
```

Environment values override application YAML. Restart the application after
changing application YAML, or recreate the server container after changing
Compose environment values. The timeout must be positive and finite; invalid
values fail settings validation.

This uses [HTTPX's network timeout](https://www.python-httpx.org/advanced/timeouts/)
for connection, read, write and connection-pool waits, not a deadline for the
whole Agent turn. It does not add retries. A timeout retains the default Session
title and records the timeout type and configured duration in administrator
Session detail and server logs.

## Verify the connection

Before making an Agent available:

- run a normal response and a tool or MCP call;
- check the errors produced by an invalid key, rate limit, and timeout;
- confirm usage and cost attribution in LiteLLM; and
- verify that the Environment's network rules admit the gateway address.

## Related guides

- [Define an Agent](authoring-agents.md)
- [Environments](environments.md)
- [Deploy AstraBox](deploy.md)
- [Protect credentials used by Agents](egress-credential-injection.md)
