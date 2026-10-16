# 快速入门

> 通过 5 个步骤运行你的第一个 AstraBox Agent。

5 步跑通你的第一个 AstraBox Agent：启动 AstraBox、选择运行环境、创建 Agent、创建 Session、收发消息。自托管服务启动后，无需安装任何 SDK。

## 前置条件

- 一台 Linux 主机（或 WSL 2），安装 Docker Engine 26.0 或更高版本与 Compose 插件
  2.17.0 或更高版本，并且当前用户可以使用 Docker socket
- 一个模型服务的 API Key：Anthropic、DeepSeek，或其他 Anthropic 兼容、OpenAI 兼容的服务
- `curl` 与 `jq`
- Web 浏览器

:::note
**Windows 用户**：以下命令使用 bash 语法。请在 WSL 2 中运行（通过 `wsl --install` 安装），Docker Desktop 的 WSL 集成会提供 Docker socket。
:::

Agent 的模型请求统一经过内置网关
[LiteLLM](https://docs.litellm.ai/)。安装脚本会写入你选择的模型服务的设置；之后可以按
[连接模型](models.md)添加更多路由，包括 OpenAI 兼容服务和本地模型，再由 Environment
选择端点和管理员管理的凭证。

受保护凭证传递默认启用：Agent 只看到占位符，内置 OpenSandbox provider 会在沙箱进程
之外，只把真实凭证加入匹配的模型请求。其他沙箱 provider 必须实现同样的受保护 Vault
传递；不具备该能力的 provider 会报告不支持。

## 第 1 步：启动 AstraBox

在 Docker 主机上运行安装脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh | bash
```

脚本会询问 Agent 使用哪个模型服务，以及该服务的 API Key 和模型 ID；然后把最新发布
版本安装到 `~/astrabox`，拉取已发布的镜像并启动，最后打印控制台地址。

在浏览器中打开 <http://127.0.0.1:8088>。

:::note
如果希望改为从源码运行，参见[从源码运行](deploy.md#run-from-a-clone)。无人值守安装、
使用保存同一批镜像的镜像仓库或主机无法访问 GitHub 时，参见[安装脚本的设置](deploy.md#installer-settings)。
:::

:::warning
本地部署不启用认证，并且只监听本机回环地址。将 AstraBox 暴露到共享或公网地址前，请先配置[团队登录](team-login.md)和 TLS。
:::

## 第 2 步：选择运行环境

打开「管理台 > 运行环境」，选择一个已启用、并提供你要使用的 Agent 程序、沙箱、网络访问和模型连接的运行环境。

:::note
如果没有合适的运行环境，请先创建一个。可配置项参见[运行环境](environments.md)。
:::

## 第 3 步：创建 Agent

打开「管理台 > Agent」，点击「创建 Agent」。填写名称，选择第 2 步中的运行环境，再选择或填写模型。系统提示词、MCP 服务、Skill、Plugin、代码仓库和其他设置都是可选项，只需配置当前 Agent 需要的能力。点击「创建」。

![在 AstraBox 控制台创建 Agent](./img/agent-create-console-zh.png)

## 第 4 步：创建 Session

打开「首页 > Agent」，找到刚刚创建的 Agent，点击「开始对话」。AstraBox 会创建一个 Session，并在网页中打开。

:::note
需要在下一步发送消息后，Agent 才会开始执行。
:::

## 第 5 步：发消息 + 收事件

输入任务，例如 `Write a Python function that calculates fibonacci numbers`，然后发送消息。页面会实时显示 Agent 的回复和其他 Session 事件。

:::note
任务运行在 AstraBox 中，而不是浏览器中。离开当前页面不会中断 Agent 的工作，之后可以从侧边栏重新打开这个 Session。
:::

## 端到端脚本

Agent 的可写字段由其 Environment 所选的 Agent 程序声明，因此 AstraBox 无法为所有部署
使用同一个固定的 Agent JSON 对象。完成第 1–3 步后，下面的脚本会按名称找到该 Agent，
创建 Session，发送一条消息，并以流式方式接收结果：

```bash
#!/usr/bin/env bash
set -euo pipefail

SERVICE_URL="${SERVICE_URL:-http://127.0.0.1:8088}"
AGENT_NAME="${1:?Usage: bash quickstart.sh <agent-name>}"

AGENT_ID=$(curl --fail --silent --show-error "$SERVICE_URL/api/v1/agents" \
  | jq -er --arg name "$AGENT_NAME" \
    '[.data[] | select(.name == $name)][0].agent_id')

SESSION_ID=$(curl --fail --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/agents/$AGENT_ID/conversations" \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: quickstart-session' \
  --data '{}' \
  | jq -er '.data.session_id')

curl --fail --silent --show-error --output /dev/null --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/inputs" \
  --header 'Content-Type: application/json' \
  --data '{
    "content": "Write a Python function that computes Fibonacci numbers and run a test.",
    "client_message_id": "quickstart-message-1"
  }'

curl --fail --no-buffer --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/stream?after_seq=0"
```

如果部署启用了团队登录，请按照[认证](api-authentication.md)中的说明，为每个请求加上
bearer token。

## 常见问题

**Q: 控制台打不开怎么办？**

A: 用 `cd ~/astrabox/containers && docker compose ps` 检查服务状态，并确认是在这台主机上打开 <http://127.0.0.1:8088>；控制台不会发布到其他地址。部署问题参见[部署 AstraBox](deploy.md)。

**Q: 为什么无法创建 Agent？**

A: 确认已经填写名称、选择运行环境并设置模型。所选运行环境必须已启用，并且支持 Agent 对话。

**Q: 为什么 Session 收不到事件？**

A: 必须发送消息才会触发 Agent 执行。如果任务失败，请打开「管理台 > 错误」查看记录的错误。

**Q: 浏览器连接中断了怎么办？**

A: 从侧边栏重新打开 Session。AstraBox 会保存 Session 及其事件流，页面可以加载已有记录并继续接收新的事件。

**Q: 创建 Agent 时为什么没有可选的运行环境？**

A: 当前部署没有已启用且支持 Agent 对话的运行环境。请按照第 2 步创建或启用一个。

## 下一步

- [定义 Agent](authoring-agents.md) — 了解 Agent 配置的全部字段
- [运行环境](environments.md) — 自定义运行环境
- [启动 Session](sessions.md) — 深入管理 Session
