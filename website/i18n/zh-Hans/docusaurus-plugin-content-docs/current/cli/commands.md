---
title: CLI 命令详解
---

# CLI 命令详解

**AstraBox CLI** 是与 **AstraBox** 部署交互的核心工具，提供了一套完整的命令，用于简化和自动化部署启动、资源配置与 **Agent** 使用流程。无论是运行本地 AstraBox、管理远程部署，还是向 Agent 发送任务，**AstraBox CLI** 都提供同一套命令。

下面按命令列出功能、参数选项、示例和使用说明。

## 命令总览

**AstraBox CLI** 遵循标准的 `astrabox <command> [arguments] [options]` 格式。

| 命令 | 功能描述 | 核心应用场景 |
| :--- | :--- | :--- |
| `init` | **初始化配置**：创建 `astrabox.yaml`，或把现有部署导出成配置文件。 | 开始使用配置文件、把现有资源纳入文件管理。 |
| `schema` | **查看资源字段**：读取部署支持的 Agent 或 Environment 字段。 | 编写有效配置、查看候选值。 |
| `get` | **查看资源**：列出一类资源或其中一项。 | 查看 Agent、Environment、Assistant、Session 和远程 MCP Server 配置。 |
| `diff` | **预览变更**：显示 `apply` 将执行的变更，不写入数据。 | 应用配置前进行检查。 |
| `apply` | **应用配置**：创建或更新文件声明的 Environment 和 Agent。 | 通过文件管理部署、接入 CI/CD。 |
| `run` | **运行 Agent**：发送任务并流式输出回复。 | 从终端或脚本使用 Agent。 |
| `status` | **查看状态**：检查部署的健康状态和就绪状态。 | 监控本地或远程部署。 |
| `destroy` | **清理资源**：删除文件声明的 Agent。 | 在明确确认后移除 Agent。 |
| `up` | **本地启动**：启动官方维护的 Compose 部署。 | 从源码目录运行 AstraBox。 |
| `down` | **本地停止**：停止官方维护的 Compose 部署。 | 停止服务或删除数据卷。 |
| `logs` | **查看本地日志**：读取或持续输出 Compose 服务日志。 | 排查本地部署问题。 |
| `mcp serve` | **提供 MCP 工具**：通过 stdio 提供部署管理命令。 | 让 MCP 客户端管理 AstraBox。 |
| `serve` | **运行 API Server**：通过 Uvicorn 启动 FastAPI 应用。 | 本地开发和运维。 |
| `verify-opensandbox-snapshots` | **验证快照**：在 OpenSandbox 上验证暂停和恢复。 | 运维人员验证部署。 |

---

## `astrabox init`

`astrabox init` 命令用于创建 `astrabox.yaml` 配置文件，支持从骨架开始和导出现有部署两种模式，可以减少手工整理配置的工作。

### 使用模式

1. **骨架模式**：不连接部署，创建一份带注释的配置文件，适合从零开始配置。
2. **导出模式**：把部署中已有的 Environment 和 Agent 整理成 `astrabox apply` 可以使用的配置文件。

### 命令格式

```bash
# 骨架模式：创建带注释的配置文件
astrabox init [options]

# 导出模式：导出现有部署
astrabox init --from-deployment [options]
```

### 核心参数

- `--file`、`-f`（可选）：
  - **说明**：配置文件的写入位置。
  - **默认值**：`astrabox.yaml`。
  - **限制**：目标文件已存在时，只有添加 `--force` 才会覆盖。

### 骨架模式选项

| 选项 | 说明 | 示例 |
| :--- | :--- | :--- |
| `--file`、`-f` | 把骨架写入其他路径。 | `--file config/astrabox.yaml` |
| `--force` | 覆盖已经存在的目标文件。 | `--force` |

### 导出模式选项

| 选项 | 说明 | 示例 |
| :--- | :--- | :--- |
| `--from-deployment` | 导出正在运行的部署，不创建骨架。 | `--from-deployment` |
| `--endpoint` | 指定部署地址。 | `--endpoint https://astrabox.example.com` |
| `--token` | 向部署发送 Bearer Token。 | `--token <access-token>` |

### 通用选项

| 选项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `--output`、`-o` | 输出格式：`table` 或 `json`。 | `table` |
| `--force` | 覆盖目标文件。 | 不启用 |

### 使用示例

#### 骨架模式

```bash
# 示例 1：在当前目录创建 astrabox.yaml
astrabox init

# 示例 2：在指定路径创建配置文件
astrabox init --file config/astrabox.yaml

# 示例 3：覆盖已经存在的文件
astrabox init --force

# 示例 4：使用路径参数简写
astrabox init -f deploy/astrabox.yaml
```

#### 导出模式

```bash
# 示例 5：导出默认的本地部署
astrabox init --from-deployment

# 示例 6：导出远程部署
astrabox init --from-deployment \
  --endpoint https://astrabox.example.com

# 示例 7：导出到指定路径
astrabox init --from-deployment \
  --file deploy/astrabox.yaml

# 示例 8：使用 Bearer Token 导出
astrabox init --from-deployment \
  --endpoint https://astrabox.example.com \
  --token <access-token>

# 示例 9：以 JSON 格式返回执行结果
astrabox init --from-deployment --output json
```

**最佳实践**

- **从骨架开始**：配置新部署时，先创建带注释的文件，再通过 `astrabox schema` 查看支持的字段。
- **使用导出模式**：部署中已经有可用的 Environment 和 Agent 时，直接导出，避免手工重新录入。
- **覆盖前先检查**：已有配置文件时，先使用其他 `--file` 路径或版本控制，再决定是否添加 `--force`。

### 运行效果

#### 骨架模式输出

执行骨架模式后，会看到类似的输出：

```text
{
  "agents": 1,
  "environments": 1,
  "file": "astrabox.yaml",
  "from_deployment": false
}
wrote astrabox.yaml
```

生成的文件包含 `version: 1`、一个示例 Environment 和一个示例 Agent。通过 `astrabox schema environment` 和 `astrabox schema agent`，可以查看当前部署接受的字段和值。

#### 导出模式输出

执行导出模式后，会看到类似的输出：

```text
{
  "agents": 2,
  "environments": 1,
  "file": "astrabox.yaml",
  "from_deployment": true
}
wrote astrabox.yaml
```

### 导出模式详解

导出模式可以把 Web 控制台中已经配置好的部署整理成 `astrabox.yaml`，不需要手工重新创建。

#### 工作原理

1. **读取字段定义**：从部署读取 Agent 和 Environment 的配置字段。
2. **读取资源**：读取部署中的 Environment 和 Agent。
3. **保留可写字段**：只保留相应字段定义中允许写入的内容。
4. **写入文件**：生成 `astrabox diff` 和 `astrabox apply` 可以使用的 `version: 1` 文档。

#### 导出文件的内容

生成的配置文件会：

- **先声明 Environment**：新部署可以先创建 Environment，再创建引用它的 Agent。
- **排除服务端字段**：ID、版本、所有者和时间戳不会写入配置文件。
- **保留已有凭证**：已经保存的密钥会以掩码形式导出，未修改掩码时，应用配置会保留原值。

#### 导出模式要求

```bash
# 已安装 AstraBox CLI
astrabox --help

# 部署可以正常访问
astrabox status
```

#### 常见问题

**问：没有正在运行的部署，也可以创建配置文件吗？**

答：可以。`astrabox init` 会直接写入带注释的骨架，不连接任何部署。

**问：为什么命令拒绝写入文件？**

答：目标文件已经存在。请选择其他路径，或检查现有文件后添加 `--force`。

**问：导出的文件可以应用到其他部署吗？**

答：可以，但目标部署需要接受相同的资源字段和候选值。请先通过 `astrabox schema` 和 `astrabox diff` 检查。

---



## `astrabox schema`

`astrabox schema` 用于读取部署在写入 Agent 或 Environment 时接受的资源字段。结果包含字段名、类型、是否必填，以及存在固定选项时的候选值。

### 使用方法

```bash
# 查看 Agent 字段
astrabox schema agent

# 查看 Environment 字段
astrabox schema environment
```

### 两类资源字段

#### 🎯 Agent 字段

Agent 字段描述一个 Agent 使用什么以及如何工作，包括名称、模型、Environment、系统提示词、MCP Server、Skill、Plugin、代码仓库和部署支持的其他设置。

```bash
astrabox schema agent
```

#### ⚡ Environment 字段

Environment 字段描述可供 Agent 使用的 Agent 程序、模型服务连接、凭证和默认设置。

```bash
astrabox schema environment
```

### 主要参数

#### 资源类型

| 参数 | 说明 | 可选值 |
| :--- | :--- | :--- |
| `kind` | 需要查看字段的资源类型。 | `agent`、`environment` |

#### 候选值

字段只有固定选项时，表格输出会在 `ENUM` 列显示候选值。JSON 输出会返回完整的字段说明，包括嵌套列表项的格式。

```bash
# 便于阅读的字段表格
astrabox schema environment

# 完整、可供程序读取的字段定义
astrabox schema environment --output json
```

候选值由部署提供。例如，`engine_kind` 用于选择已经安装的 Agent 程序，`endpoint_provider` 用于选择模型服务连接类型。

### 控制选项

| 选项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `--endpoint` | 部署地址。 | `ASTRABOX_ENDPOINT`，之后是官方维护的本地地址 |
| `--token` | Bearer Token。 | `ASTRABOX_TOKEN`，之后是 OAuth 客户端凭证 |
| `--output`、`-o` | 输出格式：`table` 或 `json`。 | `table` |

### 使用示例

#### 示例 1：查看 Agent 字段

```bash
astrabox schema agent
```

表格会显示字段名、类型、是否必填和候选值。这些字段可以写在 `astrabox.yaml` 的 `agents:` 列表中。

#### 示例 2：查看 Environment 字段

```bash
astrabox schema environment
```

这些字段可以写在 `astrabox.yaml` 的 `environments:` 列表中。

#### 示例 3：读取嵌套字段格式

```bash
astrabox schema agent --output json
```

JSON 输出包含字段说明和表格中无法完整展示的列表项格式。

#### 示例 4：查看远程部署

```bash
astrabox schema agent \
  --endpoint https://astrabox.example.com
```

#### 示例 5：使用 Bearer Token

```bash
astrabox schema environment \
  --endpoint https://astrabox.example.com \
  --token <access-token>
```

#### 示例 6：使用环境变量

```bash
export ASTRABOX_ENDPOINT=https://astrabox.example.com
export ASTRABOX_TOKEN=<access-token>
astrabox schema agent
```

#### 示例 7：在 CI/CD 中使用

```bash
astrabox schema agent --output json > agent-schema.json
astrabox schema environment --output json > environment-schema.json
```

### 配置验证

`astrabox schema` 不验证本地文件。`astrabox diff -f astrabox.yaml` 会解析文档、检查顶层结构、读取这些字段定义，并在不发送写入请求的情况下验证每一个资源字段。

### 最佳实践

1. **编写配置前先查看字段**：读取计划管理的目标部署。
2. **自动化时使用 JSON**：JSON 会保留完整的字段说明。
3. **不要在脚本中写死候选值**：从目标部署读取固定选项，不要假设每个部署都安装了相同的 Agent 程序或模型连接。
4. **先预览再应用**：修改 `astrabox.yaml` 后运行 `astrabox diff`。
5. **不要把密钥提交到代码仓库**：优先通过 Web 控制台或部署的 Secret 管理能力配置凭证；导出的密钥会显示为掩码。

---

## `astrabox get`

`astrabox get` 用于查看部署中保存的资源。它既可以列出一类资源，也可以按名称或 ID 查看其中一项。

### 使用方法

```bash
astrabox get <kind> [name] [options]
```

### 参数说明

| 参数/选项 | 说明 | 是否必填 |
| :--- | :--- | :--- |
| `kind` | 要读取的资源类型。 | 是 |
| `name` | 查看一项资源，先按名称匹配，再按 ID 匹配。 | 否 |
| `--endpoint` | 部署地址。 | 否 |
| `--token` | Bearer Token。 | 否 |
| `--output`、`-o` | 输出格式：`table` 或 `json`。 | 否 |

### 资源类型

#### 支持的类型

| 类型 | 返回内容 |
| :--- | :--- |
| `agents` | 可以创建对话并执行任务的 Agent。 |
| `environments` | 可复用的 Agent 程序与模型服务设置。 |
| `assistants` | 部署提供的 Assistant。 |
| `sessions` | 对话及其当前状态。 |
| `mcp-servers` | 部署中保存的远程 MCP Server 配置。 |

#### 完整示例

```bash
# 列出所有 Agent
astrabox get agents

# 按名称查看一个 Agent
astrabox get agents researcher

# 按 ID 查看一个 Session
astrabox get sessions <session-id>

# 返回完整的 Environment 文档
astrabox get environments --output json
```

#### 使用场景

- **修改前查看资源**：先读取当前资源，再通过 Web 控制台或 `astrabox.yaml` 修改。
- **为自动化流程取得 ID**：JSON 输出包含完整的资源文档。
- **查看对话状态**：列出 Session，可以看到 ID 和当前状态。
- **了解模型设置**：为 Agent 选择 Environment 前，先查看已有 Environment。

### 运行结果

默认表格会显示所选资源中用于识别和日常查看的字段；`--output json` 会返回完整文档。

```text
NAME        MODEL              ENVIRONMENT_NAME  ENABLED  AGENT_ID
researcher  <model-name>       default           true     <agent-id>
```

### 使用示例

```bash
# 示例 1：列出 Agent
astrabox get agents

# 示例 2：列出 Environment
astrabox get environments

# 示例 3：列出 Assistant
astrabox get assistants

# 示例 4：列出 Session
astrabox get sessions

# 示例 5：列出远程 MCP Server 配置
astrabox get mcp-servers

# 示例 6：按名称查看一项资源
astrabox get agents researcher

# 示例 7：按 ID 查看一项资源
astrabox get sessions <session-id>

# 示例 8：查看远程部署
astrabox get agents --endpoint https://astrabox.example.com

# 示例 9：以 JSON 格式输出
astrabox get agents --output json
```

### 注意事项

- 按名称或 ID 查找不到资源时，命令以退出码 1 结束。
- 多项资源匹配同一个名称时，JSON 输出会返回匹配列表，不会自行选择其中一个。
- 表格输出只保留常用字段；脚本需要嵌套设置或服务端字段时，应使用 JSON。
- `get` 只读取资源，不发送写入请求。

---

## `astrabox diff`

`astrabox diff` 用于预览应用 `astrabox.yaml` 后会发生的变化。它执行与 `astrabox apply` 相同的读取操作，但不发送写入请求。

### 使用方法

```bash
astrabox diff --file <path> [options]
```

### 参数说明

| 参数/选项 | 说明 | 是否必填 |
| :--- | :--- | :--- |
| `--file`、`-f` | `astrabox.yaml` 的路径。 | 是 |
| `--endpoint` | 部署地址。 | 否 |
| `--token` | Bearer Token。 | 否 |
| `--output`、`-o` | 输出格式：`table` 或 `json`。 | 否 |

### 对比过程

1. **读取配置**：读取并解析 YAML 文档。
2. **检查结构**：要求使用 `version: 1`、已知的顶层字段和正确的资源列表格式。
3. **读取字段定义**：根据目标部署验证每一个 Agent 和 Environment。
4. **匹配资源**：按名称匹配配置文件与部署中的资源。
5. **报告操作**：显示每项资源将被创建、更新还是保持不变。

Agent 名称存在歧义时，`diff` 会停止，不会自行选择。这个冲突处理方式与 `apply` 相同。

### 使用示例

```bash
# 示例 1：预览默认配置文件
astrabox diff --file astrabox.yaml

# 示例 2：使用简写
astrabox diff -f astrabox.yaml

# 示例 3：预览远程部署的变更
astrabox diff -f astrabox.yaml \
  --endpoint https://astrabox.example.com

# 示例 4：返回可供程序读取的结果
astrabox diff -f astrabox.yaml --output json
```

### 对比完成后

默认表格会为每项声明显示一行：

```text
KIND         NAME        ACTION     FIELDS
environment  default     unchanged
agent        researcher  update     model, system
```

- `create`：资源不存在，`apply` 将创建它。
- `update`：资源已经存在，列出的字段发生了变化。
- `unchanged`：资源已经与声明一致。

检查结果后，运行 `astrabox apply -f astrabox.yaml` 发送写入请求。

---

## `astrabox apply`

`astrabox apply` 用于创建或更新 `astrabox.yaml` 中声明的所有 Environment 和 Agent。

### 使用方法

```bash
astrabox apply --file <path> [options]
```

### 参数说明

| 参数/选项 | 说明 | 是否必填 |
| :--- | :--- | :--- |
| `--file`、`-f` | `astrabox.yaml` 的路径。 | 是 |
| `--dry-run` | 只报告将发生的变更，不发送写入请求。 | 否 |
| `--endpoint` | 部署地址。 | 否 |
| `--token` | Bearer Token。 | 否 |
| `--output`、`-o` | 输出格式：`table` 或 `json`。 | 否 |

### 执行流程

```
读取并验证 astrabox.yaml
        ↓
读取资源字段和当前资源
        ↓
先应用 Environment，再应用 Agent
        ↓
创建或更新每项声明
        ↓
返回每项资源的处理结果
```

- **Environment 会被完整替换**：需要声明所有必填字段。从 `astrabox init --from-deployment` 导出的配置开始修改，可以避免漏掉现有设置。
- **Agent 使用更新操作**：命令会发送文件中声明的字段，并携带已保存的版本；存在并发修改时会拒绝覆盖。
- **不会隐式删除**：文件中没有出现的资源会保留在部署中。需要删除已声明的 Agent 时使用 `astrabox destroy`。

### 使用示例

```bash
# 示例 1：应用默认配置文件
astrabox apply --file astrabox.yaml

# 示例 2：使用简写
astrabox apply -f astrabox.yaml

# 示例 3：通过 apply 预览
astrabox apply -f astrabox.yaml --dry-run

# 示例 4：应用到远程部署
astrabox apply -f astrabox.yaml \
  --endpoint https://astrabox.example.com

# 示例 5：在 CI/CD 中返回 JSON
astrabox apply -f astrabox.yaml --output json
```

### 为什么使用 apply

- ✅ **统一声明**：在一个文件中管理相关的 Environment 和 Agent。
- ✅ **依赖顺序**：先创建或更新 Environment，再处理引用它的 Agent。
- ✅ **冲突保护**：Agent 版本过期或名称有歧义时，命令会失败，不覆盖、不猜测。
- ✅ **结果可重复**：声明与部署已经一致时，再次执行会报告 `unchanged`。
- ✅ **支持自动化**：JSON 输出和稳定退出码可以用于脚本和 CI/CD。

---

## `astrabox run`

`astrabox run` 会通过 Agent 创建对话、发送一项任务，并持续输出回复，直到 Session 数据流关闭。也可以用它继续已有的 Session。

### 使用方法

```bash
astrabox run <agent> <task> [options]
```

### 参数说明

| 参数/选项 | 说明 | 是否必填 | 默认值 |
| :--- | :--- | :--- | :--- |
| `agent` | Agent 名称或 Agent ID。使用 `--session` 时仍需保留这个位置参数。 | 是 | — |
| `task` | 发送给 Agent 的任务。 | 是 | — |
| `--session` | 继续已有的 Session，不创建新对话。 | 否 | 创建新对话 |
| `--timeout` | 整次运行的超时时间，包括等待沙箱就绪和执行任务。 | 否 | `900` 秒 |
| `--endpoint` | 部署地址。 | 否 | 官方维护的本地地址 |
| `--token` | Bearer Token。 | 否 | 环境变量或 OAuth 客户端凭证 |
| `--output`、`-o` | 输出格式：`table` 或 `json`。 | 否 | `table` |

### 使用示例

#### 示例 1：直接发送任务

```bash
astrabox run researcher "总结本周的代码变更"
```

命令会按名称或 ID 查找 `researcher`，创建对话，等待沙箱可以接收任务，然后流式输出回复。

#### 示例 2：发送较长的任务

```bash
astrabox run researcher \
  "比较 docs/ 中的两份方案，并列出每项结论使用的依据"
```

#### 示例 3：使用远程部署

```bash
astrabox run researcher "检查尚未合并的变更" \
  --endpoint https://astrabox.example.com
```

#### 示例 4：使用 Bearer Token

```bash
astrabox run researcher "整理发布说明" \
  --endpoint https://astrabox.example.com \
  --token <access-token>
```

#### 示例 5：继续已有的 Session

```bash
astrabox run researcher "继续上一次任务" \
  --session <session-id>
```

命令格式仍要求提供 `agent` 位置参数，但任务会发送到 `--session` 指定的现有对话。

#### 示例 6：返回一份 JSON 结果

```bash
astrabox run researcher "返回最新状态" --output json
```

JSON 模式会把完整回复放进结果对象，确保 stdout 是一份可以直接解析的 JSON 文档。

#### 示例 7：缩短超时时间

```bash
astrabox run researcher "执行冒烟检查" --timeout 120
```

### 运行效果

使用默认输出时，回复文本会边生成边显示。数据流关闭后，命令会报告 Session ID，以及 Agent 是否正在等待回答。

```text
正在检查代码仓库……
发布清单还有三项未完成。
session <session-id>
```

使用 `--output json` 时，结果包含 `text`、`frames`、`errors`、`session_id` 和 `pending_interaction` 字段。

### 注意事项

1. **等待沙箱就绪**：新对话创建后，沙箱可能还没有准备好。命令会等待 Session 进入 `READY` 再发送任务。
2. **整次运行共用一个超时**：`--timeout` 同时限制沙箱等待和任务执行。
3. **数据流关闭不一定代表任务结束**：命令会再次读取 Session；Agent 正在等待回答时，会返回 `pending_interaction`。
4. **通过其他入口回答问题**：请在 Web 控制台或通过 [HTTP API](../api.md) 回答。
5. **两种输出方式不同**：默认输出会实时显示文本；JSON 输出会缓存文本，确保结果可以解析。
6. **Agent 错误会改变退出码**：数据流结果包含错误时，命令以退出码 1 结束。

---

## `astrabox status`

`astrabox status` 用于检查部署的健康状态和就绪状态。它可以连接本地或远程部署，不要求当前目录是 AstraBox 源码目录。

### 使用方法

```bash
astrabox status [options]
```

### 参数说明

| 选项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `--endpoint` | 部署地址。 | `ASTRABOX_ENDPOINT`，之后是 `http://127.0.0.1:$ASTRABOX_SERVER_HOST_PORT` |
| `--token` | Bearer Token。 | `ASTRABOX_TOKEN`，之后是 OAuth 客户端凭证 |
| `--output`、`-o` | 输出格式：`table` 或 `json`。 | `table` |

### 输出示例

#### 🏠 本地部署

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

#### ☁️ 远程部署

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

### 状态说明

| 字段 | 说明 |
| :--- | :--- |
| `endpoint` | 本次检查的部署地址。 |
| `healthy` | `/healthz` 是否成功响应。 |
| `ready` | `/readyz` 是否表示部署已经可以提供服务。 |
| `detail` | 健康检查失败时返回健康状态详情，否则返回就绪状态详情。 |

没有部署响应时，命令以退出码 4 结束；JSON 输出中会包含部署地址和检查详情。

### 使用示例

```bash
# 示例 1：查看默认的本地部署
astrabox status

# 示例 2：查看远程部署
astrabox status --endpoint https://astrabox.example.com

# 示例 3：通过环境变量指定部署
ASTRABOX_ENDPOINT=https://astrabox.example.com astrabox status

# 示例 4：返回可供程序读取的结果
astrabox status --output json
```

---

## `astrabox destroy`

`astrabox destroy` 用于删除 `astrabox.yaml` 中声明的所有 Agent。命令要求明确确认，并把文件中声明的 Environment 报告为 `retained`。

### 使用方法

```bash
astrabox destroy --file <path> --yes [options]
```

### 参数说明

| 参数/选项 | 说明 | 是否必填 |
| :--- | :--- | :--- |
| `--file`、`-f` | `astrabox.yaml` 的路径。 | 是 |
| `--yes` | 确认命令将删除正在使用的 Agent。 | 是 |
| `--endpoint` | 部署地址。 | 否 |
| `--token` | Bearer Token。 | 否 |
| `--output`、`-o` | 输出格式：`table` 或 `json`。 | 否 |

### 安全确认

命令不会显示交互式确认提示。没有 `--yes` 时会拒绝执行：

```bash
astrabox destroy -f astrabox.yaml --yes
```

确认删除前，请先检查配置文件，并执行 `astrabox diff` 或 `astrabox get agents`。

### 会删除什么

#### Agent

命令会按名称匹配 `agents:` 中的每一项，并删除已经存在的 Agent。声明的 Agent 已经不存在时，结果为 `absent`。

#### Environment

`environments:` 中的每一项都会报告为 `retained`。AstraBox 没有提供删除 Environment 的接口，因此 CLI 不会声称已经删除。

配置文件中没有声明的资源不会受到影响。

### 运行效果

```text
KIND         NAME        ACTION    FIELDS
agent        researcher  delete
environment  default     retained
```

### 使用示例

```bash
# 示例 1：删除文件中声明的 Agent
astrabox destroy --file astrabox.yaml --yes

# 示例 2：使用简写
astrabox destroy -f astrabox.yaml --yes

# 示例 3：删除远程部署中声明的 Agent
astrabox destroy -f astrabox.yaml --yes \
  --endpoint https://astrabox.example.com

# 示例 4：返回可供程序读取的结果
astrabox destroy -f astrabox.yaml --yes --output json
```

### 重要提示

1. **apply 不会自动删除**：从 `astrabox.yaml` 移除 Agent 不会删除部署中的 Agent。需要删除时，请使用包含该 Agent 声明的文件执行 `destroy`。
2. **匹配时不需要完整 Agent 内容**：命令根据 `name` 匹配，但配置文件仍需通过文档和资源字段检查。
3. **名称有歧义时会失败**：部署中有多个同名 Agent 时，命令返回冲突，不会自行选择。
4. **不支持删除 Environment**：Environment 会保留在部署中。
5. **保留可恢复的声明**：如果以后可能重新创建 Agent，请把经过检查的配置文件保存到版本控制中。

---

## 通用选项

### --help 查看帮助

所有命令都支持 `--help`：

```bash
# 查看某个命令的帮助
astrabox apply --help
astrabox run --help

# 查看所有命令
astrabox --help
```

### 连接选项

需要访问部署的命令使用相同的连接选项：

| 选项 | 说明 |
| :--- | :--- |
| `--endpoint` | 部署地址。 |
| `--token` | 本次执行使用的 Bearer Token。 |
| `--output`、`-o` | 面向用户的 `table` 输出，或面向脚本的 `json` 输出。 |

部署地址按以下顺序确定：

1. `--endpoint`
2. `ASTRABOX_ENDPOINT`
3. `http://127.0.0.1:$ASTRABOX_SERVER_HOST_PORT`；变量未设置时端口为 `8088`

直接通过 `astrabox serve` 启动的 Server 默认使用 `8000` 端口。连接这个进程时，应设置 `--endpoint http://127.0.0.1:8000`。

### 鉴权 {#authentication}

默认本地身份模式不需要凭证。部署启用鉴权后，CLI 按以下顺序使用凭证：

1. `--token`
2. `ASTRABOX_TOKEN`
3. OAuth 客户端凭证

使用 OAuth 客户端凭证时，需要同时设置三个必填变量：

```bash
export ASTRABOX_CLIENT_ID=<client-id>
export ASTRABOX_CLIENT_SECRET=<client-secret>
export ASTRABOX_TOKEN_URL=https://identity.example.com/oauth/token
export ASTRABOX_SCOPE=astrabox:admin  # 可选
astrabox get agents
```

OAuth 配置不完整时，命令以退出码 2 结束，并列出缺少的变量。只有设置了 `ASTRABOX_SCOPE` 时，CLI 才会在换取 Token 时发送 Scope。

`apply`、`diff` 和 `destroy` 会访问管理接口，因此用于这些命令的 OAuth 客户端需要 `astrabox:admin` Scope。Scope 和 Token 规则见 [API 鉴权](../api-authentication.md)。

### 输出格式和退出码

资源、Agent 和状态命令都接受面向用户的默认输出和可供程序读取的 JSON 输出；默认输出的具体格式取决于命令。使用 `--output json` 时，成功和失败都会返回 JSON。

`astrabox logs` 是例外：日志本身就是结果，因此无论选择哪种输出格式，命令都会输出原始 Compose 日志。`astrabox mcp serve` 则会在 stdout 中写入 MCP 协议消息。

| 退出码 | 含义 |
| :--- | :--- |
| `0` | 执行成功。 |
| `1` | 部署拒绝请求、资源不存在，或 Agent 执行结果包含错误。 |
| `2` | 命令参数错误，或配置文件无法读取、格式无效。 |
| `3` | 鉴权或权限检查失败。 |
| `4` | 无法访问部署、Token 地址或 Session 数据流，或超过就绪等待时间。 |
| `5` | 命令无法在不猜测的情况下继续，例如名称有歧义或 Agent 版本过期。 |

API 请求失败时，JSON 输出会在 `code` 字段中保留部署注册的错误码。

---

## 平台服务命令

除了资源命令，AstraBox CLI 还提供本地部署、MCP 集成和运维命令。

### `astrabox up`

在 AstraBox 源码目录中启动官方维护的 Compose 部署，并默认等待服务就绪。

```bash
# 启动部署
astrabox up

# 启动前重新构建服务镜像
astrabox up --build

# 修改就绪等待时间
astrabox up --wait-seconds 600

# Compose 启动服务后立即返回
astrabox up --no-wait

# 返回可供程序读取的启动结果
astrabox up --output json
```

启动前检查会确认当前目录包含 AstraBox 源码、Docker CLI 与 Daemon 可用，并且官方维护的 Compose 文件完整。执行结果会报告源码目录、Docker Server 版本、访问地址、就绪结果和 Agent 沙箱镜像状态。默认 Agent 沙箱镜像不存在时，请运行 `make build-agent-image`。部署使用其他 Agent 沙箱镜像时，请设置 `ASTRABOX_AGENT_IMAGE`。

### `astrabox down`

在 AstraBox 源码目录中停止官方维护的 Compose 部署。

```bash
# 停止服务，保留数据卷
astrabox down

# 停止服务，并删除数据库和所有命名数据卷中的状态
astrabox down --volumes
```

`--volumes` 会删除数据。没有这个选项时，命名数据卷会保留到下次启动。

### `astrabox logs`

读取官方维护的 Compose 部署日志。

```bash
# 显示每项服务最后 200 行日志
astrabox logs

# 查看一项服务
astrabox logs server

# 修改日志行数
astrabox logs server --tail 100

# 持续输出所有服务的新日志
astrabox logs --follow
```

可选参数使用 Compose 服务名，例如 `server`、`postgres`、`redis`、`sandbox-edge` 和 `sandbox-dns-edge`。即使添加 `--output json`，日志也保持原始文本格式。

### `astrabox mcp serve`

通过 stdio 把 AstraBox 管理操作作为 MCP 工具提供给其他应用。

```bash
astrabox mcp serve

astrabox mcp serve \
  --endpoint https://astrabox.example.com
```

MCP Server 通过 stdin/stdout 使用逐行 JSON-RPC，并提供以下工具：

| 工具 | 操作 |
| :--- | :--- |
| `astrabox_schema` | 读取 Agent 或 Environment 配置字段。 |
| `astrabox_get` | 列出一类资源。 |
| `astrabox_export` | 把 Environment 和 Agent 导出成 `astrabox.yaml` 文档。 |
| `astrabox_diff` | 预览文档变更，不写入数据。 |
| `astrabox_apply` | 创建或更新文档声明的资源。 |
| `astrabox_status` | 检查部署的健康状态和就绪状态。 |
| `astrabox_run` | 创建或继续对话，并发送一项任务。 |

请在客户端中把它注册为 stdio MCP Server。这个命令用于管理 AstraBox 部署；部署自身的 `/api/v1/mcp` 接口则用于[调用已经通过 MCP 提供的 Agent](../agent-mcp.md)。

### 运维命令

#### `astrabox serve`

通过 Uvicorn 启动 AstraBox FastAPI 应用。这是一个直接运行的 Server 进程，不是官方维护的 Compose 部署。

```bash
astrabox serve
astrabox serve --host 0.0.0.0 --port 8000
astrabox serve --reload --log-level debug
```

| 选项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `--host` | 监听地址。 | `ASTRABOX_HOST`，之后是 `127.0.0.1` |
| `--port` | 监听端口。 | `ASTRABOX_PORT`，之后是 `8000` |
| `--reload` | 代码变更后自动重载，只用于开发。 | 不启用 |
| `--log-level` | Uvicorn 日志级别。 | `ASTRABOX_LOG_LEVEL`，之后是 `info` |

当监听地址不是 loopback 且 `ASTRABOX_WEB_IDENTITY` 为 `none` 时，这个命令会拒绝在没有鉴权的情况下启动。请配置支持的身份认证方式；只有部署所在网络已经阻止未授权访问时，才显式设置 `ASTRABOX_ALLOW_UNAUTHENTICATED_BIND=1`。

#### `astrabox verify-opensandbox-snapshots`

创建一个真实沙箱，写入标记，暂停沙箱，恢复同一个沙箱，读取标记，最后删除测试沙箱。

```bash
astrabox verify-opensandbox-snapshots

astrabox verify-opensandbox-snapshots \
  --image <agent-image> \
  --lifecycle-base-url http://127.0.0.1:8080 \
  --timeout-seconds 720 \
  --json-out snapshot-evidence.json
```

这个运维命令会连接真实的 OpenSandbox 生命周期服务。没有传入 `--image` 时会使用 `ASTRABOX_AGENT_IMAGE`；超时时间必须在 1 到 720 秒之间；`--json-out` 会以原子方式写入不含密钥的验证结果。

---

## 常用工作流

### 📝 完整的本地部署流程

适合从源码目录启动 AstraBox，再通过配置文件管理：

```bash
# 1️⃣ 安装软件包
make install

# 2️⃣ 构建 Agent 沙箱镜像
make build-agent-image

# 3️⃣ 启动官方维护的部署
.venv/bin/astrabox up

# 4️⃣ 检查就绪状态
.venv/bin/astrabox status

# 5️⃣ 创建或导出配置文件
.venv/bin/astrabox init --from-deployment

# 6️⃣ 预览并应用修改
.venv/bin/astrabox diff -f astrabox.yaml
.venv/bin/astrabox apply -f astrabox.yaml

# 7️⃣ 向 Agent 发送任务
.venv/bin/astrabox run <agent-name> "检查部署状态"
```

执行最后一条命令前，请先通过 Web 控制台或 `astrabox.yaml` 连接模型服务并创建 Agent。

### 🔄 管理已有的远程部署

适合 AstraBox 已经运行在其他主机上的情况：

```bash
# 1️⃣ 设置部署地址和凭证
export ASTRABOX_ENDPOINT=https://astrabox.example.com
export ASTRABOX_TOKEN=<access-token>

# 2️⃣ 检查部署
astrabox status

# 3️⃣ 导出当前配置
astrabox init --from-deployment --file remote.astrabox.yaml

# 4️⃣ 检查修改
astrabox diff -f remote.astrabox.yaml

# 5️⃣ 应用修改
astrabox apply -f remote.astrabox.yaml

# 6️⃣ 使用 Agent
astrabox run <agent-name> "报告代码仓库的当前状态"
```

### 🔄 快速迭代流程

修改 `astrabox.yaml` 后：

```bash
# 方式 1：先 diff，再 apply
astrabox diff -f astrabox.yaml
astrabox apply -f astrabox.yaml

# 方式 2：先使用 apply 的 dry-run，再正式 apply
astrabox apply -f astrabox.yaml --dry-run
astrabox apply -f astrabox.yaml
```

同一个 Agent 被其他用户修改后，版本冲突会阻止本次更新。请重新执行 `diff`，检查最新状态后再应用。

### 🌍 管理多个部署

为每个部署使用独立的配置文件和访问地址：

```bash
# 开发环境
astrabox diff -f development.astrabox.yaml \
  --endpoint https://dev.astrabox.example.com
astrabox apply -f development.astrabox.yaml \
  --endpoint https://dev.astrabox.example.com

# 生产环境
astrabox diff -f production.astrabox.yaml \
  --endpoint https://astrabox.example.com
astrabox apply -f production.astrabox.yaml \
  --endpoint https://astrabox.example.com
```

配置文件本身不保存目标地址；确实需要时，也可以用同一份声明检查多个部署。

---

## 常见问题

### ❌ 找不到配置文件

**错误信息：**

```
astrabox.yaml: file does not exist
```

**解决办法：**

```bash
# 新配置
astrabox init

# 部署中已经有 Environment 和 Agent
astrabox init --from-deployment

# 或指定已有文件
astrabox diff --file path/to/astrabox.yaml
```

### ❌ Docker 没有运行（本地部署）

**错误信息：**

```
astrabox: Docker is installed but its daemon is not answering
```

**解决办法：**

1. 启动 Docker Desktop 或 Docker Engine 服务。
2. 确认 `docker info` 可以成功执行。
3. 在 AstraBox 源码目录中重新运行 `astrabox up`。

`up`、`down` 和 `logs` 需要源码目录，因为官方维护的 Compose 配置保存在代码仓库中。

### ❌ 远程部署没有配置鉴权

**错误信息：**

```
astrabox: deployment refused the request with HTTP 401
```

**解决办法：**

```bash
# 使用 Bearer Token
export ASTRABOX_TOKEN=<access-token>

# 或配置 OAuth 客户端凭证
export ASTRABOX_CLIENT_ID=<client-id>
export ASTRABOX_CLIENT_SECRET=<client-secret>
export ASTRABOX_TOKEN_URL=https://identity.example.com/oauth/token
export ASTRABOX_SCOPE=astrabox:admin  # 可选
```

OAuth 客户端凭证只配置了一部分时，命令以退出码 2 结束，并列出缺少的变量。

### ❌ apply 执行失败

请根据退出码和 JSON 错误判断原因，不要匹配错误文案：

```bash
astrabox apply -f astrabox.yaml --output json
```

常见原因包括：

1. 文件不是有效的 YAML，或使用了不支持的文档版本。
2. 资源包含目标部署不接受的配置字段。
3. Environment 没有包含完整替换所需的必填字段。
4. 部署中存在多个同名 Agent，无法确定要修改哪一个。
5. CLI 读取 Agent 后又发生了其他修改，版本检查拒绝了过期更新。

修改配置或检查最新资源状态后，请重新运行 `astrabox diff -f astrabox.yaml`。

### 💡 调试技巧

```bash
# 查看命令格式
astrabox apply --help

# 检查部署能否访问以及是否就绪
astrabox status --output json

# 查看可以配置的资源字段
astrabox schema agent --output json
astrabox schema environment --output json

# 查看完整资源文档
astrabox get agents --output json
astrabox get environments --output json

# 查看本地 Server 日志
astrabox logs server --tail 200
```

本地启动失败时，`astrabox up` 会分别报告源码文件、Docker、就绪状态和 Agent 沙箱镜像的问题。

### CLI 不负责管理的内容

- **Deployment 绑定**：CLI 没有 `astrabox get deployments`，`apply` 也不配置 Channel 或定时任务绑定。请在 Web 控制台中配置，参阅 [Deployment](../deployments.md)。
- **回答 Agent 的待处理问题**：`run` 会报告 Agent 正在等待回答，但不能提交答案。请使用 Web 控制台或 [HTTP API](../api.md)。
- **非 Compose 部署的生命周期**：`up`、`down` 和 `logs` 只操作官方维护的 Compose 部署。Kubernetes 或已有 OpenSandbox 的部署方式见[部署 AstraBox](../deploy.md)；部署启动后，`status`、`get` 和 `apply` 等客户端命令仍可使用。

---

## 下一步

- 📖 [配置文件说明](./configuration.md) - 深入了解文件结构和资源字段
- 🚀 [快速开始](../quickstart.md) - 启动 AstraBox 并创建第一个 Agent
- 🛠️ [部署 AstraBox](../deploy.md) - 配置和运行自行部署的 AstraBox
