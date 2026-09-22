# Overview

> Run installed Agent programs in self-hosted cloud sandboxes.

AstraBox is an open-source, self-hosted runtime that turns installed Agent programs into cloud Agents available around the clock. You don't have to build your own agent loop, manage tool execution sandboxes, or handle long-lived connections. Define an Agent and start a Session in the console or via API, and complex tasks run in the cloud while results stream back in real time.

## Core Concepts

| Concept         | Description                                                                           | Analogy                   |
| --------------- | ------------------------------------------------------------------------------------- | ------------------------- |
| **Agent**       | A cloud Agent powered by an installed Agent program                                    | "Cloud teammate"          |
| **Environment** | The runtime for a Session, including the Agent program, sandbox, model connection, and network configuration | "Desk and toolbox" |
| **Session**     | One stateful Agent execution, including its messages, Events, and current state         | "A specific piece of work" |
| **Event**       | The real-time event stream produced by a Session                                      | "Live progress feed"      |

## Workflow

1. **Define an Agent.** Specify the model, system prompt, and extensions.
2. **Configure an Environment.** Choose the Agent program, sandbox, model connection, and network configuration.
3. **Start a Session.** Use the Agent to create a runtime instance.
4. **Send message + Stream events.** Send a message to the Session and stream the Agent's messages, progress, and status changes over HTTP.

## Enterprise Infrastructure Included

One deployment brings up the pieces a team usually assembles by hand:

- **Model gateway.** The bundled [LiteLLM](https://github.com/BerriAI/litellm) gateway is on by default. Agents select a route name; upstream keys, routing, budgets and request logs stay on the server, and any Anthropic, OpenAI-compatible or local provider can sit behind it. See [Connect a model service](models.md).
- **Team login.** [Casdoor](https://github.com/casdoor/casdoor) is pre-integrated as the identity provider, with OIDC, organizations and roles, and sign-in through providers such as DingTalk, WeCom, Feishu or GitHub. One Compose overlay turns it on; see [Set up team login](team-login.md).
- **Isolated sandboxes.** [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) runs them on one Docker host or a Kubernetes cluster, and warm capacity lets conversations start and resume in seconds.
- **Credentials outside the sandbox.** Vault credentials are injected at the sandbox's egress boundary; the Agent only sees a placeholder. See [Protect credentials used by Agents](egress-credential-injection.md).
- **Triggers and channels.** Schedules, signed webhooks and messaging platforms start Agents without a person present. See [Automate Agent runs](deployments.md).

## Verify Connectivity

```bash
# Verify the local API and list all Agents
curl -s http://127.0.0.1:8088/api/v1/agents
```

A successful response looks like:

```json
{
  "code": "OK",
  "message": "success",
  "data": []
}
```

## When to Use AstraBox Agents

- **Long-running asynchronous tasks** — code review, large refactors, automated test generation.
- **API integration** — embed agent capabilities in backend services without maintaining a separate runtime.
- **Batch processing** — fan out parallel Sessions to handle bulk requests.
- **Scheduled jobs** — create a scheduled Deployment to run periodic inspections or reports.

## Authentication

When a team deployment uses OAuth access tokens, API requests include the following header:

| Header          | Value                   | Description                                             |
| --------------- | ----------------------- | ------------------------------------------------------- |
| `Authorization` | `Bearer <access token>` | Access token issued by the configured identity provider |

:::note
Local mode does not enable API authentication and is intended for loopback access only. Team deployments use the configured identity provider, such as OIDC, verified JWTs, trusted gateway headers, or an installed provider. See [Team authentication](team-login.md).
:::

## Pagination

List endpoints use resource-specific pagination. See [Pagination](api-pagination.md) for the supported cursor and page-number forms.

## FAQ

**Q: Can I use AstraBox and the Agent program locally at the same time?**

A: Yes. Local use is best for interactive development on one computer; an AstraBox Agent can be accessed remotely, execute long-running tasks, and connect to other systems. They complement each other.

**Q: How many Sessions can a single Agent run concurrently?**

A: The same Agent can run multiple active Sessions. The practical limit is the compute and sandbox capacity of your deployment.

**Q: How is data secured?**

A: By default, each Session runs in an isolated sandbox; an Environment can also use a supported Agent-shared sandbox. Session history is stored outside the sandbox, and workspace lifetime follows the Environment's idle action and storage configuration.

## Next Steps

- [Quickstart](quickstart.md) — get your first Agent running.
- [Defining an Agent](authoring-agents.md) — dive deeper into Agent configuration.
- [Environments](environments.md) — configure the runtime environment.
- [Sessions](sessions.md) — manage Session lifecycle.
