<p align="center">
  <img src="website/static/img/astrabox-mark.svg" alt="AstraBox" width="72" height="72" />
</p>

<h1 align="center">AstraBox</h1>

<p align="center">
  <strong>The open-source, self-hosted alternative to Claude Managed Agents.</strong>
</p>

<p align="center">
  Open source · Self-hosted · Apache-2.0
</p>

<p align="center">
  <a href="https://github.com/Colton-z/AstraBox/actions/workflows/ci.yml"><img src="https://github.com/Colton-z/AstraBox/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="Apache-2.0" /></a>
  <a href="https://www.astrabox.ai/"><img src="https://img.shields.io/badge/docs-English-4f46e5" alt="Documentation" /></a>
  <a href="https://www.astrabox.ai/zh-Hans/"><img src="https://img.shields.io/badge/文档-简体中文-4f46e5" alt="Chinese documentation" /></a>
</p>

<p align="center">
  <strong>English</strong> · <a href="README.zh-CN.md">简体中文</a>
</p>

Turn the Agent programs you already use into cloud Agents that stay available
24/7. AstraBox runs Claude Code, Codex, Hermes, DeepSeek Harness and Pi as
managed Agents on your own infrastructure, with any model. Conversations start
and resume in seconds; Sessions, sandboxes, credentials and history stay under
your control.

You do not have to turn an Agent program into a service yourself, manage
sandbox lifecycle, or maintain long-lived connections. Deploy AstraBox, create
an Agent in the web console, and start a Session. Complex tasks run in a cloud
sandbox while results stream back in real time.

AstraBox runs the Agent programs you already use as cloud Agents that can be
reached remotely, continue long-running work, and connect to your applications,
automations, and messaging platforms. The web console, API, Session records,
authentication, and sandboxes all run on infrastructure you control.

## Core concepts

| Concept | Description | Analogy |
| --- | --- | --- |
| **Agent** | A cloud Agent powered by an installed Agent program | "Cloud teammate" |
| **Environment** | The Agent program, sandbox, model connection, network access, and lifecycle used for a Session | "Desk and toolbox" |
| **Session** | One stateful Agent execution, including its messages, Events, and current state | "A specific piece of work" |
| **Event** | The real-time output and state changes produced by a Session | "Live progress feed" |

Through these resources, developers can run interactive or long-running tasks,
connect remote or local MCP servers, Plugins, Skills, and repositories, trigger
Agents from schedules, webhooks, APIs, and messaging platforms, and protect
access with authentication, authorization, isolated sandboxes, and managed
credentials.

[Explore AstraBox capabilities →](docs/capabilities.md)

## Enterprise infrastructure included

One deployment brings up the pieces a team usually assembles by hand:

- **Model gateway** — [LiteLLM](https://github.com/BerriAI/litellm) is bundled
  and on by default: one route name per model, upstream keys held on the
  server, budgets and usage logs, and any Anthropic, OpenAI-compatible or local
  provider behind it. See [Connect a model](docs/models.md).
- **Team login** — [Casdoor](https://github.com/casdoor/casdoor) is
  pre-integrated as the identity provider: OIDC, organizations and roles, and
  sign-in through providers such as DingTalk, WeCom, Feishu or GitHub. Turn it
  on with one Compose overlay; see [Team login](docs/team-login.md).
- **Isolated sandboxes** — [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox)
  on one Docker host or a Kubernetes cluster, with warm capacity so
  conversations start in seconds.
- **Credentials outside the sandbox** — Vault credentials are injected at the
  sandbox's egress boundary; the Agent only ever sees a placeholder.
- **Channels and triggers** — schedules, signed webhooks and messaging
  platforms through official [Satori](https://github.com/satorijs/satori)
  adapters.

## Included Agent programs

| Agent program | Sandbox image | Used for |
| --- | --- | --- |
| Claude Code | `ghcr.io/colton-z/astrabox-sandbox-claude-code` | Agent |
| Codex | `ghcr.io/colton-z/astrabox-sandbox-codex` | Agent |
| DeepSeek Harness | `ghcr.io/colton-z/astrabox-sandbox-deepseek-harness` | Agent |
| pi | `ghcr.io/colton-z/astrabox-sandbox-pi` | Agent |
| Hermes Agent | `ghcr.io/colton-z/astrabox-sandbox-hermes` | Assistant |

Other Agent programs can be added with a compatible sandbox image. See
[Add an Agent program](docs/writing-an-engine-adapter.md).

## Workflow

1. **Deploy AstraBox** — run the service and OpenSandbox on one Docker host,
   Kubernetes, or infrastructure you already operate.
2. **Configure an Environment** — choose the Agent program, sandbox image, model
   connection, network access, and lifecycle.
3. **Create an Agent** — select the Environment and model in the web console,
   then add a system prompt, MCP servers, Plugins, Skills, or a repository only
   when the Agent needs them.
4. **Start a Session** — open the Agent and start a Session.
5. **Send messages and receive Events** — follow live output, answer questions
   or approvals, and return later without keeping the original browser open.

## Quickstart

### Prerequisites

- A Linux host (or WSL 2) running Docker Engine with the Compose plugin, v2 or
  later, and a user that can use the Docker socket
- An API key for a model service: Anthropic, DeepSeek, or another Anthropic- or
  OpenAI-compatible service

Install the latest release with one command:

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh | bash
```

The installer asks which model service your Agents use, installs the deployment
into `~/astrabox`, pulls the published images, starts them, and prints the
console address once the console answers. Open <http://127.0.0.1:8088>. Select
an Environment, create an Agent, and start your first Session from the console.

![Create an Agent in the AstraBox console](docs/img/agent-create-console-en.png)

The local deployment listens on loopback and does not require login. Configure
[team authentication](https://www.astrabox.ai/docs/team-login) and TLS before
exposing it to another network.

Run the installer again to upgrade: it installs the latest release over the
current one and keeps your Sessions, credentials and settings.

### Run from a clone

Building the images from a checkout takes longer and is the path for changing
AstraBox itself:

```bash
git clone https://github.com/Colton-z/AstraBox.git
cd AstraBox

make build-agent-image

export ANTHROPIC_API_KEY="your-anthropic-api-key"
export ANTHROPIC_MODEL="your-model-name"
scripts/compose.sh up --build -d
```

For the complete setup and API alternative, see the
[Quickstart](https://www.astrabox.ai/docs/quickstart). For the installer settings,
Kubernetes, or an existing OpenSandbox service, see
[Deploy AstraBox](https://www.astrabox.ai/docs/deploy).

Prewarming prepares the Agent runtime before a Session claims it. Native
conversation state is stored in the platform database; a persistent workspace
volume is optional and preserves task files separately. For multiple API
replicas or sandbox nodes, see [distributed deployment](docs/deploy-distributed.md)
and [workspace storage](docs/deploy.md#where-conversation-workspaces-live).

## When to use AstraBox

- **Long-running asynchronous tasks** — let work continue after the developer's
  computer or browser disconnects.
- **API integration** — use an Agent from an application without building and
  operating a separate Agent runtime.
- **Batch processing** — run multiple Sessions for independent requests.
- **Scheduled and event-driven work** — start Agents from a schedule, webhook,
  external system, or messaging platform.

Local Agent programs remain the best fit for interactive development on one
computer. AstraBox makes the same kind of Agent available remotely and to other
systems; the two approaches complement each other.

## Documentation

- [Overview](docs/overview.md)
- [Quickstart](docs/quickstart.md)
- [Define an Agent](docs/authoring-agents.md)
- [Run a Session](docs/sessions.md)
- [Connect MCP servers, Plugins, and Skills](docs/adding-tools.md)
- [Automate Agent work](docs/deployments.md)
- [Connect messaging platforms](docs/channels.md)
- [Deploy AstraBox](docs/deploy.md)
- [HTTP API](docs/api.md)

## Development

```bash
make install
make build-agent-image
make build-assistant-image
make dev
```

Open <http://127.0.0.1:5173>. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
maintained workflow.

## Contributing

Issues and pull requests are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgements

AstraBox stands on the shoulders of these open-source projects:
[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox),
[LiteLLM](https://github.com/BerriAI/litellm),
[Casdoor](https://github.com/casdoor/casdoor),
[Satori](https://github.com/satorijs/satori),
[DBOS Transact](https://github.com/dbos-inc/dbos-transact-py),
[mergerfs](https://github.com/trapexit/mergerfs),
[AIO Sandbox](https://github.com/agent-infra/sandbox),
[shadcn/ui](https://github.com/shadcn-ui/ui),
[Vercel AI SDK and AI Elements](https://github.com/vercel/ai) and
[Docusaurus](https://github.com/facebook/docusaurus) — and it runs the Agent
programs [Codex](https://github.com/openai/codex),
[Hermes Agent](https://github.com/NousResearch/hermes-agent),
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness),
[Pi](https://github.com/earendil-works/pi) and
[Claude Code](https://github.com/anthropics/claude-code). [NOTICE](NOTICE) lists
every attribution and license.

## License

Apache License 2.0. See [LICENSE](LICENSE).

Claude and Claude Code are trademarks of Anthropic; OpenAI and Codex are
trademarks of OpenAI. AstraBox is an independent project, not affiliated with
or endorsed by them. Claude Code is proprietary software used under Anthropic's
terms; see [NOTICE](NOTICE).
