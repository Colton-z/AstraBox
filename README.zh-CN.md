<p align="center">
  <img src="website/static/img/astrabox-mark.svg" alt="AstraBox" width="72" height="72" />
</p>

<h1 align="center">AstraBox</h1>

<p align="center">
  <strong>Claude Managed Agents 的开源、自托管替代。</strong>
</p>

<p align="center">
  开源 · 自行部署 · Apache-2.0
</p>

<p align="center">
  <a href="https://github.com/Colton-z/AstraBox/actions/workflows/ci.yml"><img src="https://github.com/Colton-z/AstraBox/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="Apache-2.0" /></a>
  <a href="https://www.astrabox.ai/"><img src="https://img.shields.io/badge/docs-English-4f46e5" alt="英文文档" /></a>
  <a href="https://www.astrabox.ai/zh-Hans/"><img src="https://img.shields.io/badge/文档-简体中文-4f46e5" alt="中文文档" /></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <strong>简体中文</strong>
</p>

把你已经在使用的 Agent 程序变成 7×24 小时在线的云端 Agent。AstraBox 在你自己的基础设施上，
把 Claude Code、Codex、Hermes、DeepSeek Harness 和 Pi 作为托管 Agent 运行，模型任选。
对话秒级拉起、秒级恢复；会话、沙箱、凭证和历史都留在你自己手里。

你无需自己把 Agent 程序改造成服务、管理沙箱生命周期或处理长连接——部署 AstraBox，
在网页控制台创建 Agent 并启动 Session，即可让复杂任务在云端沙箱中执行并实时接收结果。

AstraBox 把你已经在使用的 Agent 程序变成可以远程访问、持续执行长时间任务并接入应用、
自动化流程和消息平台的云端 Agent。网页控制台、API、Session 记录、身份验证和沙箱都
运行在你管理的基础设施中。

## 核心概念

| 概念 | 说明 | 类比 |
| --- | --- | --- |
| **Agent** | 由已安装 Agent 程序驱动的云端 Agent | “云端同事” |
| **Environment** | Session 使用的 Agent 程序、沙箱、模型连接、网络访问和生命周期设置 | “办公桌和工具箱” |
| **Session** | Agent 的一次有状态运行，包含消息、Event 和当前状态 | “一项具体工作” |
| **Event** | Session 产生的实时输出和状态变化 | “工作进度实时播报” |

开发者可以通过这些资源运行交互任务和长时间任务，连接远程或本地 MCP 服务器、Plugin、
Skill 和代码仓库，通过定时任务、Webhook、API 和消息平台触发 Agent，并使用身份验证、
鉴权、隔离沙箱和托管凭证保护访问与数据。

[查看 AstraBox 能力 →](https://www.astrabox.ai/zh-Hans/docs/capabilities)

## 开箱即用的企业级基建

一次部署就带齐团队通常要自己拼装的几块：

- **模型网关**——默认内置 [LiteLLM](https://github.com/BerriAI/litellm)：每个模型一个路由名，
  上游密钥留在服务端，支持预算与用量日志，后面可以接 Anthropic、OpenAI 兼容服务或本地模型。
  参见[连接模型服务](https://www.astrabox.ai/zh-Hans/docs/models)。
- **团队登录**——预集成 [Casdoor](https://github.com/casdoor/casdoor) 作为身份提供方：
  OIDC、组织与角色，可用钉钉、企业微信、飞书、GitHub 等账号登录。加一个 Compose 覆盖文件即可开启，
  参见[团队身份验证](https://www.astrabox.ai/zh-Hans/docs/team-login)。
- **隔离沙箱**——基于 [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox)，单台 Docker 主机
  或 Kubernetes 集群都行，并预留热容量，对话秒级拉起。
- **凭证不进沙箱**——Vault 凭证在沙箱的出站边界注入，Agent 只看得到占位符。
- **消息通道与触发器**——定时任务、签名 Webhook，以及基于官方
  [Satori](https://github.com/satorijs/satori) 适配器接入的消息平台。

## 在终端里管理，也可以交给编程助手

`astrabox` 命令行工具无需打开网页控制台，就能配置和使用本地或远程部署。命令结果可以输出为
JSON，Claude Code、Codex 等编程助手也能替你运行这些命令。

- **配置即代码**——`astrabox init --from-deployment` 把部署中的 Environment 和 Agent 导出为
  `astrabox.yaml`；`astrabox diff` 预览修改，`astrabox apply` 创建或更新资源，从不删除。
- **在命令行派发任务**——`astrabox run <agent> "<task>"` 会启动 Session、发送任务并流式输出回复；
  加上 `--session` 可以继续已有的 Session。
- **便于程序读取的输出**——加上 `--output json` 后，命令无论成功还是失败都只输出一个 JSON 对象，
  每类失败还有各自的退出码。
- **MCP 工具**——`astrabox mcp serve` 通过 stdio 把 schema、get、export、diff、apply、status 和 run
  提供为 MCP 工具。
- **本地或远程**——`astrabox up`、`down` 和 `logs` 在源码目录中运行本地 Compose 部署；其他命令
  通过 `--endpoint` 加 Bearer Token 或 OAuth 客户端凭证连接任意部署。

在源码目录中运行 `make install` 即可安装 CLI。Deployment（定时任务、Webhook 和消息平台触发）
以及对 Agent 提问的回答，需要在网页控制台或 HTTP API 中处理。详见
[CLI 概览](https://www.astrabox.ai/zh-Hans/docs/cli/overview)。

## 内置 Agent 程序

| Agent 程序 | 沙箱镜像 | 用于创建 |
| --- | --- | --- |
| Claude Code | `ghcr.io/colton-z/astrabox-sandbox-claude-code` | Agent |
| Codex | `ghcr.io/colton-z/astrabox-sandbox-codex` | Agent |
| DeepSeek Harness | `ghcr.io/colton-z/astrabox-sandbox-deepseek-harness` | Agent |
| pi | `ghcr.io/colton-z/astrabox-sandbox-pi` | Agent |
| Hermes Agent | `ghcr.io/colton-z/astrabox-sandbox-hermes` | Assistant |

还可以通过兼容的沙箱镜像接入其他 Agent 程序。详见
[接入新的 Agent 程序](https://www.astrabox.ai/zh-Hans/docs/writing-an-engine-adapter)。

## 为扩展而设计

AstraBox 中每个可替换的部分都是一个 Python 接口，并有对应的插件注册点。安装的 Python 包把实现
注册到相应的插件组，AstraBox 按名称选用；名称不存在时会直接报错，不会退回默认实现。

| 扩展接口 | 插件组 | 内置实现 |
| --- | --- | --- |
| Agent 程序 | `astrabox.providers.engine` | 随 AstraBox 提供的全部 Agent 程序 |
| 沙箱后端 | `astrabox.providers.sandbox` | 运行在 Docker 或 Kubernetes 上的 OpenSandbox |
| 模型服务 | `astrabox.providers.model` | LiteLLM 网关 |
| 消息平台 | `astrabox.providers.channel` | 消息平台网关、通用 JSON Webhook |
| 身份提供方 | `astrabox.web.identity` | 本地模式、OIDC、已验证 JWT、可信身份请求头 |
| Secret Store | `astrabox.providers.secrets` | 本地加密、AWS KMS |
| 数据存储 | `astrabox.providers.repository` | PostgreSQL、MongoDB、SQLite |
| 工作区存储 | `astrabox.providers.storage` | 挂载卷、Amazon EFS |
| 远程 MCP 服务来源 | `astrabox.providers.extensions` | 管理员在 AstraBox 中保存的配置、LiteLLM MCP 网关 |

- **接口版本**——`astrabox.providers.*` 下的插件可以用 `seams_api_version` 声明构建时依据的接口
  版本；与当前 AstraBox 不一致时，AstraBox 会拒绝加载。
- **一致性测试套件**——`astrabox.testing` 为沙箱后端、工作区存储、消息平台、Agent 程序和数据存储
  提供可复用的测试套件，在插件自己的测试中引用即可。
- **应用扩展**——通过 API 路由、中间件、生命周期钩子和服务实现的插件组扩展应用本身。

详见[接入新的 Agent 程序](https://www.astrabox.ai/zh-Hans/docs/writing-an-engine-adapter)、
[接入新的消息平台](https://www.astrabox.ai/zh-Hans/docs/writing-a-channel-provider)、
[在 Python 应用中使用 AstraBox](https://www.astrabox.ai/zh-Hans/docs/embedding)和
[扩展接口](https://www.astrabox.ai/zh-Hans/docs/architecture#plugin-interfaces)。

## 工作流程

1. **部署 AstraBox**——在一台 Docker 主机、Kubernetes 或已有基础设施中运行 AstraBox
   和 OpenSandbox。
2. **配置 Environment**——选择 Agent 程序、沙箱镜像、模型连接、网络访问和
   生命周期。
3. **创建 Agent**——在网页控制台选择 Environment 和模型；只有 Agent 需要时，才添加
   系统提示词、MCP 服务器、Plugin、Skill 或代码仓库。
4. **启动 Session**——打开 Agent，启动一个 Session。
5. **发送消息并接收 Event**——查看实时输出，回答问题或审批；浏览器关闭后，
   稍后仍可回来继续。

## 快速开始

### 前置条件

- 一台 Linux 主机（或 WSL 2），安装 Docker Engine 与 Compose 插件 v2 或更高版本，
  并且当前用户可以使用 Docker socket
- 一个模型服务的 API Key：Anthropic、DeepSeek，或其他 Anthropic 兼容、
  OpenAI 兼容的服务

一条命令安装最新发布版本：

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh | bash
```

安装脚本会询问 Agent 使用哪个模型服务，把部署安装到 `~/astrabox`，拉取已发布的镜像
并启动，控制台能够响应后打印其地址。打开 <http://127.0.0.1:8088>，选择 Environment，
在控制台创建 Agent，然后启动第一个 Session。

![在 AstraBox 控制台创建 Agent](website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/img/agent-create-console-zh.png)

本地部署只监听 loopback，并且不要求登录。向其他网络开放前，请先配置
[团队身份验证](https://www.astrabox.ai/zh-Hans/docs/team-login)和 TLS。

升级时再次运行安装脚本：它会在原处安装最新发布版本，并保留 Session、凭证和设置。

### 从源码运行

从检出的源码构建镜像耗时更长，适合需要修改 AstraBox 本身的场景：

```bash
git clone https://github.com/Colton-z/AstraBox.git
cd AstraBox

make build-agent-image

export ANTHROPIC_API_KEY="your-anthropic-api-key"
export ANTHROPIC_MODEL="your-model-name"
scripts/compose.sh up --build -d
```

完整步骤和 API 用法参见[快速开始](https://www.astrabox.ai/zh-Hans/docs/quickstart)。
安装脚本的全部设置，以及 Kubernetes 或已有 OpenSandbox 服务的用法，参见
[部署 AstraBox](https://www.astrabox.ai/zh-Hans/docs/deploy)。

预热会在 Session 领取之前准备好 Agent 运行时。原生会话状态保存在平台数据库中；持久
工作区卷是可选能力，单独负责保留任务文件。部署多个 API 副本或沙箱节点时，参见
[分布式部署](https://www.astrabox.ai/zh-Hans/docs/deploy-distributed)和
[工作区存储](https://www.astrabox.ai/zh-Hans/docs/deploy#where-conversation-workspaces-live)。

## 适用场景

- **长时间异步任务**——开发者电脑或浏览器断开后，任务仍可继续执行。
- **API 集成**——在应用中使用 Agent，不需要另外开发和维护 Agent 运行平台。
- **批量处理**——使用多个 Session 分别处理独立请求。
- **定时与事件触发任务**——通过定时任务、Webhook、外部系统或消息平台启动 Agent。

本地 Agent 程序仍然适合在一台电脑上交互开发；AstraBox Agent 则可以远程访问、执行
长时间任务并接入其他系统，两者互补。

## 文档

- [概览](https://www.astrabox.ai/zh-Hans/docs/overview)
- [快速开始](https://www.astrabox.ai/zh-Hans/docs/quickstart)
- [定义 Agent](https://www.astrabox.ai/zh-Hans/docs/authoring-agents)
- [运行 Session](https://www.astrabox.ai/zh-Hans/docs/sessions)
- [连接 MCP 服务器、Plugin 和 Skill](https://www.astrabox.ai/zh-Hans/docs/adding-tools)
- [自动运行 Agent](https://www.astrabox.ai/zh-Hans/docs/deployments)
- [接入消息平台](https://www.astrabox.ai/zh-Hans/docs/channels)
- [部署 AstraBox](https://www.astrabox.ai/zh-Hans/docs/deploy)
- [HTTP API](https://www.astrabox.ai/zh-Hans/docs/api)

## 开发

```bash
make install
make build-agent-image
make build-assistant-image
make dev
```

打开 <http://127.0.0.1:5173>。维护流程参见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 参与贡献

欢迎提交 Issue 和 Pull Request。请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 致谢

AstraBox 建立在这些开源项目之上：
[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox)、
[LiteLLM](https://github.com/BerriAI/litellm)、
[Casdoor](https://github.com/casdoor/casdoor)、
[Satori](https://github.com/satorijs/satori)、
[DBOS Transact](https://github.com/dbos-inc/dbos-transact-py)、
[mergerfs](https://github.com/trapexit/mergerfs)、
[AIO Sandbox](https://github.com/agent-infra/sandbox)、
[shadcn/ui](https://github.com/shadcn-ui/ui)、
[Vercel AI SDK 与 AI Elements](https://github.com/vercel/ai) 和
[Docusaurus](https://github.com/facebook/docusaurus)；并运行这些 Agent 程序：
[Codex](https://github.com/openai/codex)、
[Hermes Agent](https://github.com/NousResearch/hermes-agent)、
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)、
[Pi](https://github.com/earendil-works/pi) 和
[Claude Code](https://github.com/anthropics/claude-code)。完整署名与许可证见
[NOTICE](NOTICE)。

## 许可证

本项目使用 Apache License 2.0，详见 [LICENSE](LICENSE)。

Claude 与 Claude Code 是 Anthropic 的商标，OpenAI 与 Codex 是 OpenAI 的商标。AstraBox 是独立项目，
与上述公司无隶属或背书关系。Claude Code 是按 Anthropic 条款使用的专有软件，详见 [NOTICE](NOTICE)。
