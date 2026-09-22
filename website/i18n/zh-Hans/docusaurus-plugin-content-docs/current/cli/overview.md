# AstraBox CLI 概览

**AstraBox CLI** 是一个为开发者设计的命令行工具，用于简化 **Agent** 的部署和管理。无论您使用官方维护的本地部署，还是远程自行部署的 AstraBox，`astrabox` 命令都能提供一致的使用方式。

## 核心优势

- **声明式配置**：通过一个 `astrabox.yaml` 文件管理 Environment 和 Agent，清晰、可移植且易于版本控制。
- **本地与远程使用**：使用同一套资源和 Agent 命令管理官方维护的本地部署或远程自行部署的 AstraBox。
- **一键启动**：`astrabox up` 会检查本地依赖、启动官方维护的 Compose 部署并等待服务就绪。
- **适合脚本调用**：资源、状态和 Agent 命令提供 JSON 输出和稳定的退出码，便于在脚本和 CI/CD 流程中使用。
- **MCP 集成**：`astrabox mcp serve` 可以把管理操作作为 MCP 工具提供给 MCP 客户端。

## 主要命令

`astrabox` CLI 提供了一系列命令来管理 AstraBox 部署和其中的 **Agent**：

### 资源与 Agent 命令
| 命令 | 功能描述 |
| :--- | :--- |
| `astrabox init` | 创建 `astrabox.yaml` 文件，或从部署中导出一份。 |
| `astrabox schema` | 查看 Environment 或 Agent 可以配置的字段。 |
| `astrabox get` | 列出 Agent、Environment、Assistant、Session 或远程 MCP Server 配置。 |
| `astrabox diff` | 预览 `astrabox.yaml` 声明的变更。 |
| `astrabox apply` | 创建或更新 `astrabox.yaml` 声明的 Environment 和 Agent。 |
| `astrabox destroy` | 删除 `astrabox.yaml` 声明的 Agent。 |
| `astrabox run` | 向 Agent 发送任务并流式输出回复。 |
| `astrabox status` | 查看部署的健康状态、就绪状态和访问地址。 |

### 部署与集成命令
| 命令 | 功能描述 |
| :--- | :--- |
| `astrabox up` | 启动官方维护的本地部署并等待服务就绪。 |
| `astrabox down` | 停止官方维护的本地部署。 |
| `astrabox logs` | 查看官方维护的本地部署日志。 |
| `astrabox mcp serve` | 通过 stdio 把管理操作作为 MCP 工具提供给其他应用。 |

> 想要了解每个命令的详细用法？请查阅 [命令详解](./commands.md)。

## CLI 的三种使用方式

**AstraBox CLI** 可以在源码目录中使用，也可以作为远程部署的客户端使用，还可以作为 MCP Server 接入其他应用。

### 1. 本地部署

在 AstraBox 源码目录中启动并管理官方维护的 Compose 部署。

- **工作流**: `源码目录` → `构建 Agent 沙箱镜像` → `启动 Compose 部署` → `配置并运行 Agent`
- **要求**: Docker、AstraBox 源码，以及通过 `make install` 安装的软件包。

### 2. 远程部署

把客户端命令连接到任何网络可达的 AstraBox 部署。

- **工作流**: `已安装的 AstraBox CLI` → `远程 HTTP 或 HTTPS 地址` → `配置并运行 Agent`
- **要求**: 部署地址；如果部署启用了鉴权，还需要 Bearer Token 或 OAuth 客户端凭证。

### 3. MCP 客户端

当其他应用需要通过工具而不是 Shell 命令管理 AstraBox 时，把 `astrabox mcp serve` 作为 stdio MCP Server 运行。

- **工作流**: `MCP 客户端` → `AstraBox 管理工具` → `本地或远程部署`
- **优势**: AI 助手可以查看配置字段、读取资源、应用配置、检查状态并运行 Agent，而不必解析终端输出。

## 配置文件 (`astrabox.yaml`)

`astrabox.yaml` 以声明式方式定义 **AstraBox** 部署中应当存在的 Environment 和 Agent。使用 `astrabox diff` 预览变更，再使用 `astrabox apply` 创建或更新这些资源。

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
      调研指定主题，并列出使用的资料来源。
    enabled: true
```

`engine_kind` 是选择已安装 Agent 程序的配置字段，`endpoint_provider` 用于选择模型服务连接。文件规则请查阅 [配置详解](./configuration.md)；当前部署支持哪些字段，可以通过 `astrabox schema environment` 和 `astrabox schema agent` 查看。

## 快速上手

只需几分钟，即可启动 AstraBox，并通过 CLI 使用您的第一个 **Agent**。

```bash
# 1. 安装 AstraBox 并构建 Agent 沙箱镜像
make install
make build-agent-image

# 2. 启动官方维护的本地部署
.venv/bin/astrabox up

# 3. 检查部署状态
.venv/bin/astrabox status

# 4. 导出当前的 Environment 和 Agent
.venv/bin/astrabox init --from-deployment

# 5. 预览并应用对 astrabox.yaml 的修改
.venv/bin/astrabox diff -f astrabox.yaml
.venv/bin/astrabox apply -f astrabox.yaml

# 6. 向 Agent 发送任务
.venv/bin/astrabox run <agent-name> "总结本周的代码变更"
```

执行最后一条命令前，请先连接模型服务并创建 Agent。您可以使用 Web 控制台，也可以根据 `astrabox schema` 显示的字段修改 `astrabox.yaml`。

### 探索更多功能

```bash
# 以 JSON 格式读取完整的 Agent 文档
astrabox get agents --output json

# 继续已有的 Session
astrabox run <agent-name> "继续这个任务" --session <session-id>

# 让 MCP 客户端管理部署
astrabox mcp serve --endpoint https://astrabox.example.com
```

## 环境要求

### ✅ 基础环境（所有模式）
- Python 3.12 或更高版本
- 已安装 AstraBox 软件包
- 网络可达的 AstraBox 部署

### 🐍 Python 开发环境
- 在源码目录中运行 `make install`，创建 `.venv` 并安装 CLI。
- 如果已经安装 AstraBox 软件包，可以直接运行 `astrabox`。

### 📜 脚本和 CI/CD
- 资源、状态和 Agent 命令可以配合 `--output json` 使用。
- 根据文档中的退出码区分参数、鉴权、连接和冲突错误。

### 🐳 本地部署
- 使用 AstraBox 源码目录，并确保 Docker 已启动。
- 第一次启动 Agent 前，运行 `make build-agent-image` 构建 Agent 沙箱镜像。

### ☁️ 远程部署
- 设置部署地址和鉴权凭证：

  ```bash
  export ASTRABOX_ENDPOINT=https://astrabox.example.com
  export ASTRABOX_TOKEN=<access-token>
  astrabox status
  ```

- 也可以使用 OAuth 客户端凭证代替 Bearer Token。参阅[命令详解](./commands.md#authentication)。

## 下一步

- 📖 [**命令详解**](./commands.md)：深入了解每个 CLI 命令的参数和用法。
- ⚙️ [**配置详解**](./configuration.md)：掌握 `astrabox.yaml` 的文件规则和资源字段。
- 🚀 [**快速入门**](../quickstart.md)：按照端到端教程启动 AstraBox 并创建第一个 Agent。
