# 配置文件详解

`astrabox.yaml` 是 AstraBox 部署的核心配置文件，用于声明希望部署提供的 Environment 和 Agent。

## 配置系统概览

AstraBox 使用一份配置文件声明两类资源：

| 资源 | 作用 |
|------|------|
| **Environment** | 选择 Agent 程序、模型服务连接、沙箱运行方式、凭证，以及多个 Agent 可以共用的默认设置。 |
| **Agent** | 定义一个 Agent 的模型、系统提示词、MCP Server、Skill、Plugin、代码仓库、使用方式和其他设置。 |

配置文件会应用到 `--endpoint` 或 `ASTRABOX_ENDPOINT` 指定的部署，不保存部署地址或 CLI 鉴权凭证。

使用 `astrabox diff -f astrabox.yaml` 预览结果，再使用 `astrabox apply -f astrabox.yaml` 创建或更新文件声明的资源。

## 文件结构

配置文件包含三个顶层字段：

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

**三个部分**：
- **version** - 配置文件版本；当前只接受 `1`
- **environments** - 可选的 Environment 声明列表
- **agents** - 可选的 Agent 声明列表

---

## 文档顶层结构

文档顶层用于声明配置版本和两类资源列表。

### 配置示例

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

### 配置项详解

#### `version`（必填）

**配置文件版本**

- 📝 **作用**：选择 CLI 读取的顶层文件格式
- ✅ **规则**：必须是整数 `1`
- 🎯 **用于**：当文件结构超出当前 CLI 的理解范围时直接拒绝执行

**示例**：

```yaml
version: 1
```

这个值是 `astrabox.yaml` 的文档版本，不是 AstraBox 发布版本，也不是 Agent 的并发控制版本。

#### `environments`（可选）

**Environment 声明列表**

- 📝 **作用**：列出部署中应当存在的可复用运行环境和模型服务设置
- ✅ **规则**：必须是对象列表，每一项都需要非空的 `name`
- ✅ **默认值**：不填写时为空列表
- 🎯 **用于**：在处理 Agent 前创建或更新 Environment

**示例**：

```yaml
environments:
  - name: default
    engine_kind: <agent-program-id>
    endpoint_provider: <model-connection-id>
    enabled: true
```

写入 Environment 时会完整替换现有配置。应保留该 Environment 的所有必填字段，建议从 `astrabox init --from-deployment` 导出的内容开始修改。

#### `agents`（可选）

**Agent 声明列表**

- 📝 **作用**：列出需要创建或更新的 Agent
- ✅ **规则**：必须是对象列表，每一项都需要非空的 `name`
- ✅ **默认值**：不填写时为空列表
- 🎯 **用于**：配置每个 Agent 使用什么、可以访问什么，以及通过哪些方式提供服务

**示例**：

```yaml
agents:
  - name: researcher
    model: <model-name>
    environment_name: default
    system: |
      调研指定主题，并列出使用的资料来源。
    enabled: true
```

更新 Agent 时只会写入文件中声明的字段。CLI 会携带 Agent 当前保存的版本，并发修改发生时会拒绝覆盖。

#### `name`（每项资源必填）

**资源名称**

- 📝 **作用**：在部署尚未生成 ID 时标识一项声明
- ✅ **规则**：不能为空，同一类资源列表中不能重复
- 🎯 **用于**：
  - 匹配已有 Environment
  - 匹配已有 Agent
  - 报告每项 `diff`、`apply` 或 `destroy` 操作

**示例**：

```yaml
environments:
  - name: default

agents:
  - name: researcher
```

API 允许部署中存在多个同名 Agent。一个声明因此无法确定目标时，CLI 会以退出码 5 结束，并列出匹配的 Agent ID，不会自行选择。

#### 未知的顶层字段

文档顶层只接受 `version`、`environments` 和 `agents`。字段拼写错误或出现多余字段时，CLI 会在发送任何请求前失败。

```yaml
version: 1
agent: []  # 错误：正确字段是 agents
```

Environment 和 Agent 内部的字段也会经过验证，具体内容见后续两节。

---

## Environment 配置

Environment 是一组可复用的 Agent 程序、模型服务、沙箱、网络、凭证和链路追踪设置。Agent 通过 `environment_name` 选择 Environment。

### 配置示例

```yaml
environments:
  - name: default
    display_name: 默认环境
    description: 通用 Agent 运行环境
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

请把尖括号中的值替换为目标部署支持的值。

### 配置项详解

#### `engine_kind`

选择在沙箱中运行的 Agent 程序。

- **是否必填**：是
- **候选值**：目标部署已经安装的 Agent 程序
- **查看方式**：`astrabox schema environment`

不要直接照搬其他部署中的值。Plugin 可以为某一个部署添加 Agent 程序，不代表其他部署也已经安装。

#### `endpoint_provider`

选择这个 Environment 使用的模型服务连接类型。

- **是否必填**：否
- **候选值**：目标部署注册的模型连接
- **配合使用**：`provider_access`

#### `sandbox_backend`

选择用于创建 Agent 沙箱的后端。

- **是否必填**：否
- **候选值**：目标部署已经安装的沙箱后端

`runtime_template_name` 用于选择沙箱后端提供的运行模板。

#### `provider_access`

配置这个 Environment 中的 Agent 如何访问模型服务：

```yaml
provider_access:
  base_url: https://models.example.com
  api_key_secret_name: model-api-key
```

| 字段 | 说明 |
| :--- | :--- |
| `base_url` | 模型服务或模型网关地址。 |
| `api_key` | 直接填写的模型凭证；读取时会显示为掩码。 |
| `api_key_secret_name` | AstraBox 服务进程环境中凭证的逻辑名称。 |

根据模型连接支持的方式选择直接填写凭证或引用环境变量 Secret。逻辑名称会转为大写，并将短横线改为下划线：`model-api-key` 对应 `MODEL_API_KEY`。此查找不会读取 Web 控制台的 Vault。不要把明文凭证提交到版本控制。

#### `networking`

控制 Agent 沙箱的出站网络：

```yaml
networking:
  type: limited
  allowed_hosts:
    - models.example.com
  allow_mcp_servers: true
```

| 字段 | 说明 |
| :--- | :--- |
| `type` | 部署支持的网络模式。 |
| `allowed_hosts` | 在受限模式中额外允许访问的域名。 |
| `allow_mcp_servers` | 允许访问 Agent 已配置的 MCP Server 所需地址。 |

#### `idle_action`

控制沙箱空闲后如何处理。部署只接受当前沙箱后端能够支持的操作。

#### `sandbox_tenancy` / `sandbox_permission_level`

`sandbox_tenancy` 用于选择一个沙箱只属于一段对话，还是可以由同一个 Agent 使用。`sandbox_permission_level` 用于选择沙箱内部获得的权限级别。

Agent 级沙箱复用需要能够落实隔离要求的权限级别。不支持的组合会在写入 Environment 时失败。

#### `tracing` {#tracing}

配置 Agent 程序发送的链路追踪数据：

```yaml
tracing:
  enabled: true
  endpoint: https://otel.example.com
  environment: production
  signals:
    - traces
```

这个配置声明链路追踪地址、Header、直接填写的鉴权信息、环境标签、信号类型，以及是否记录用户提示词。所选 Agent 程序无法发送链路追踪数据时，部署会拒绝启用。示例假设 Collector 不要求鉴权。

Collector 需要鉴权时，填写 `auth_token` 或 `auth_token_secret_name`，二者只能选一个。凭证值须包含完整的 `Authorization` 方案，例如 `Bearer your-token` 或 `Basic base64-value`。Secret 名称从 AstraBox 服务进程的环境变量解析：`otel-auth` 对应 `OTEL_AUTH`，即转为大写并将连字符替换为下划线。它不引用 Web 控制台中的 Vault 条目。

Claude Code 支持这项配置。解析后的凭证通过 `OTEL_EXPORTER_OTLP_HEADERS` 传入 CLI，不经过出站 Vault 替换；使用 HTTPS 保护传输中的凭证。关闭链路追踪时不会读取 Secret。启用时若引用缺失或为空，AstraBox 会记录 Environment 和引用名称，并为该运行时配置禁用链路追踪，不使对话失败，也不匿名发送数据。已保存的 Environment 不会被修改。

#### 展示和状态字段

| 字段 | 说明 |
| :--- | :--- |
| `name` | Agent 通过 `environment_name` 引用的稳定名称，必填。 |
| `display_name` | 向用户显示的名称。 |
| `description` | Environment 的用途。 |
| `enabled` | Environment 是否可以使用。 |

### 自动管理的字段

未填写时，部署会补全已经确定的默认值，例如空闲处理方式和网络结构。`astrabox init --from-deployment` 会导出部署当前保存的结果。

ID、时间戳、所有者和其他服务端管理的状态不会出现在 `astrabox.yaml` 中。

---

## Agent 配置

Agent 把模型、系统提示词、MCP Server、Skill、Plugin、代码仓库和 Environment 组合成一个云端 Agent。只要 AstraBox 部署正在运行，就可以随时使用这个 Agent。

### 配置示例

```yaml
agents:
  - name: researcher
    display_name: 研究助手
    description: 调研指定主题并列出资料来源
    model: <model-name>
    system: |
      调研指定主题，并列出使用的资料来源。
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

请替换尖括号中的值，并删除不需要的可选字段。

### 字面值

`astrabox.yaml` 按照 YAML 读取。CLI 不会替换文件中的 Shell 变量或模板表达式。

```yaml
model: <model-name>       # 文档占位符，需要替换
model: ${MODEL_NAME}      # 会作为普通文本读取，不会展开环境变量
```

部署地址和 CLI 鉴权凭证通过命令参数或进程环境变量设置。Agent 运行所需的凭证应保存在 Environment 的 `provider_access` 或 AstraBox Vault 中，不要通过模板写进配置文件。

### 不使用 `Auto`

AstraBox 的 `astrabox.yaml` 不使用 `Auto` 关键字。希望部署使用默认值时，请省略可选字段；必填字段必须明确填写目标部署接受的值。

### 配置项详解

#### `name`

必填的稳定名称，`astrabox.yaml` 使用它匹配已有 Agent。服务端生成的 `agent_id` 和 `version` 不是配置字段。

#### `model`

必填的模型名称或模型路由名称，由选中的 Environment 使用。自行部署的模型网关可以提供 AstraBox 无法统一枚举的名称，因此这个字段允许直接填写文本。

```yaml
model: <model-name>
```

#### `system`

可选的系统提示词，提供给 Agent 程序：

```yaml
system: |
  认真检查代码仓库。
  每项结论都要说明依据。
```

#### `environment_name`

必填的 Environment 名称。同一份文件中的 Environment 会先应用，因此一个文档可以先创建 Environment，再创建使用它的 Agent。

#### `engine_options`

所选 Agent 程序定义的可选设置。AstraBox 会把这些设置交给 Agent 程序，不会再创造一套平行术语。

```yaml
engine_options:
  <agent-program-option>: <value>
```

只填写该 Environment 所选 Agent 程序支持的选项。

#### `skills`

Agent 可以使用的 Skill 列表：

```yaml
skills:
  - <skill-reference>
```

简单 Agent 可以不填写。这里的 Skill 会与已安装 Plugin 提供的 Skill 合并。

#### `mcp_servers`

Agent 自己使用的 MCP Server 配置，以名称作为 Key：

```yaml
mcp_servers:
  source-control:
    <server-setting>: <value>
```

配置格式遵循所选 Agent 程序的 MCP 支持。通过 AstraBox 注册表分配的远程 MCP Server 会与 Agent 自己的配置合并。

#### `default_repo` / `plugin_repos`

`default_repo` 用于检出 Agent 的主代码仓库，`plugin_repos` 用于添加提供 Plugin 的代码仓库。

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

代码仓库配置支持 `url`、`protocol`、协议要求时填写的部署密钥 Secret 名称、分支和检出深度。部署密钥名称从 AstraBox 服务进程环境解析，不读取 Web 控制台的 Vault。Plugin 仓库还可以通过 `sha` 固定 Commit，并用 `plugin_paths` 选择 Plugin 目录。

#### `exposure_mode`

控制其他应用如何使用 Agent：

| 值 | 效果 |
| :--- | :--- |
| `chat_only` | 可以创建对话。 |
| `mcp_only` | 可以通过部署的 Agent MCP 接口使用。 |
| `both` | 两种方式都可以使用。 |

#### `idle_hibernate_seconds` / `prewarm_enabled`

`idle_hibernate_seconds` 控制 Agent 空闲多长时间后休眠沙箱。`prewarm_enabled` 要求 AstraBox 为这个 Agent 保持一个完整的准备运行时；所选沙箱部署必须支持准备容量。

#### 展示和状态字段

| 字段 | 说明 |
| :--- | :--- |
| `display_name` | 向用户显示的名称。 |
| `description` | Agent 的用途。 |
| `icon` | 与 Agent 一起显示的图标引用。 |
| `tags` | 用于整理 Agent 的标签。 |
| `use_cases` | 向用户展示的示例任务。 |
| `enabled` | Agent 是否可以使用。 |

### 自动管理的字段

部署会生成 `agent_id`、时间戳、所有者和用于并发控制的 `version`。这些字段不会出现在导出的配置中，也不应添加到 `astrabox.yaml`。

---

## 同时应用两类资源

一份配置文件可以同时声明 Environment 和使用这些 Environment 的 Agent。CLI 会按照依赖顺序处理。

### 配置示例

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
      调研指定主题，并列出使用的资料来源。
    enabled: true
```

### 两类资源的区别

| 处理方式 | Environment | Agent |
| :--- | :--- | :--- |
| 文件中的标识 | `name` | `name` |
| 应用顺序 | 先处理 | Environment 之后处理 |
| 创建或更新 | 通过同一个 PUT 按名称创建或更新 | 按名称匹配后创建或更新 |
| 更新内容 | 完整替换 | 文件声明的字段，加上已保存版本 |
| 并发修改 | 写入最新的完整声明 | 版本过期时拒绝写入 |
| destroy | 保留，没有删除接口 | 文件中声明且提供 `--yes` 时删除 |

### 配置项详解

#### 应用顺序

无论两份 YAML 列表在文件中的视觉位置如何，Environment 都会先于 Agent 处理。因此，Agent 的 `environment_name` 可以引用同一次 apply 中创建的 Environment。

#### 资源匹配

ID 由部署创建，不保存在 `astrabox.yaml` 中。CLI 根据 `name` 匹配资源。

Environment 名称就是资源 Key。API 不要求 Agent 名称在整个部署中唯一；存在多个同名 Agent 时，CLI 会返回冲突，不会随机选择。

#### 不会隐式删除

`astrabox apply` 会创建或更新声明，但不会删除已经从文件中移除的资源。

```bash
# 预览并应用创建或更新
astrabox diff -f astrabox.yaml
astrabox apply -f astrabox.yaml

# 明确删除这个文件声明的 Agent
astrabox destroy -f astrabox.yaml --yes
```

### 自动管理的字段

更新 Agent 时，CLI 会读取并发送它已经保存的 `version`。创建资源时，部署会生成 ID、所有者、时间戳和其他服务端状态。

---

## 资源字段定义

`astrabox.yaml` 的三个顶层字段是固定的。Environment 和 Agent 内部使用哪些字段，由接收配置的 AstraBox 部署定义。

### 配置示例

```bash
# 便于阅读的表格
astrabox schema environment
astrabox schema agent

# 用于脚本和嵌套对象的完整字段定义
astrabox schema environment --output json
astrabox schema agent --output json
```

表格中的一行类似：

```text
KEY               TYPE      REQUIRED  ENUM
name              string    true
engine_kind       enum      true      <已经安装的 Agent 程序>
endpoint_provider enum      false     <已经安装的模型连接>
```

### 配置项详解

#### `key`

写入 Environment 或 Agent 声明的 YAML 字段名。

#### `type`

部署要求的值类型。常见类型包括字符串、多行文本、整数、布尔值、固定选项、字符串列表、对象、对象列表和 Environment 引用。

#### `required`

写入这类资源时是否必须提供该字段。Environment 使用完整替换，因此声明中需要保留所有必填字段。

#### `enum`

固定选项的候选值。Agent 程序、沙箱后端、权限和模型连接可以因部署而异，因为已安装的 Plugin 可以扩展这些能力。

#### `item_schema`

结构化对象或列表项的字段定义，用于描述 Environment 网络、模型服务访问、链路追踪和代码仓库等嵌套配置。

#### `path`

字段在资源文档中的保存位置与 YAML Key 不同时，`path` 会给出实际位置。CLI 在执行 `diff` 时会按这个路径读取，避免服务端嵌套保存的展示字段被反复报告为变更。

#### `default`

不填写字段时由部署提供的默认值。默认值属于目标部署，CLI 不会自行补充资源默认值。

### Agent 程序定义的开放配置

`engine_options` 和 Agent 自己的 `mcp_servers` 配置内容遵循所选 Agent 程序的约定。AstraBox 会原样传递这些设置，不会为第三方定义的能力另造一套字段。

---

## 部署连接配置

目标部署和 CLI 鉴权凭证不写在 `astrabox.yaml` 中。请通过命令参数或进程环境变量提供，这样一份资源声明不会绑定到某个地址或访问 Token。

### 配置文件位置

AstraBox 没有用户级 CLI 配置文件。连接设置来自当前命令和它的进程环境。

### 配置示例

```bash
# Bearer Token
export ASTRABOX_ENDPOINT=https://astrabox.example.com
export ASTRABOX_TOKEN=<access-token>
astrabox diff -f astrabox.yaml

# OAuth 客户端凭证
export ASTRABOX_ENDPOINT=https://astrabox.example.com
export ASTRABOX_CLIENT_ID=<client-id>
export ASTRABOX_CLIENT_SECRET=<client-secret>
export ASTRABOX_TOKEN_URL=https://identity.example.com/oauth/token
export ASTRABOX_SCOPE=astrabox:admin  # 可选
astrabox apply -f astrabox.yaml
```

### 配置优先级

**部署地址**：

```
--endpoint > ASTRABOX_ENDPOINT > 官方维护的本地地址
```

**鉴权凭证**：

```
--token > ASTRABOX_TOKEN > OAuth 客户端凭证 > 不带凭证的请求
```

OAuth 客户端凭证需要同时设置 `ASTRABOX_CLIENT_ID`、`ASTRABOX_CLIENT_SECRET` 和 `ASTRABOX_TOKEN_URL`。`ASTRABOX_SCOPE` 可选。

### 使用场景

**把配置应用到指定部署**：

```bash
ASTRABOX_ENDPOINT=https://dev.astrabox.example.com \
  astrabox diff -f astrabox.yaml
```

**只为一条命令覆盖地址**：

```bash
astrabox apply -f astrabox.yaml \
  --endpoint https://astrabox.example.com
```

不要把 CLI Access Token、OAuth 客户端密钥或目标部署地址写进 `astrabox.yaml`。

---

## 最佳实践

### 🌍 管理多个部署

确实存在差异的部署可以使用不同文件：

```
config/
├── development.astrabox.yaml
├── staging.astrabox.yaml
└── production.astrabox.yaml
```

```bash
# 开发环境
astrabox diff -f config/development.astrabox.yaml \
  --endpoint https://dev.astrabox.example.com

# 生产环境
astrabox diff -f config/production.astrabox.yaml \
  --endpoint https://astrabox.example.com
```

部署地址保留在文件之外，执行命令时可以明确看到本次写入的目标。

### 🔐 安全管理敏感信息

**不要提交明文凭证**：

```yaml
# ❌ 不要提交
provider_access:
  api_key: <plaintext-model-key>

# ✅ 引用 AstraBox 服务环境中的凭证
provider_access:
  api_key_secret_name: production-model-key

# ✅ 引用 AstraBox 服务环境中的代码仓库部署密钥
default_repo:
  url: git@example.com:team/application.git
  protocol: ssh
  deploy_key_secret_name: application-deploy-key
```

对于以上示例，请通过部署的受保护密钥配置，为 AstraBox 服务设置 `PRODUCTION_MODEL_KEY` 和 `APPLICATION_DEPLOY_KEY`。Web 控制台的 Vault 分配是另一种凭证机制。CLI 鉴权信息应放在 `ASTRABOX_TOKEN` 或 OAuth 环境变量中，不写入 `astrabox.yaml`。

Environment 中直接保存了凭证时，读取结果和 `astrabox init --from-deployment` 导出内容会显示掩码。把未修改的掩码重新应用到同一个 Environment，会保留已经保存的凭证。

在本地准备的文件确实包含明文密钥时，应排除在版本控制之外：

```gitignore
# .gitignore
*.private.astrabox.yaml
```

提交不含密钥的声明或模板：

```yaml
# astrabox.yaml
provider_access:
  api_key_secret_name: production-model-key
```

### 📝 添加配置注释

YAML 注释可以解释某项设置存在的原因：

```yaml
environments:
  - name: restricted
    # 只允许访问模型网关和已配置的远程 MCP Server。
    networking:
      type: limited
      allowed_hosts:
        - models.example.com
      allow_mcp_servers: true
```

`diff` 和 `apply` 只读取文件，不会重写，因此注释会保留。`init --from-deployment` 会写入一份新的导出文件，不会保留另一份文件中的手工注释。

### ✅ 定期验证配置

```bash
# 方式 1：查看支持的字段
astrabox schema environment
astrabox schema agent

# 方式 2：解析、验证并预览，不写入数据
astrabox diff -f astrabox.yaml

# 方式 3：在 CI 中返回 JSON 预览
astrabox diff -f astrabox.yaml --output json
```

CLI 会在发送写入请求前检查完整文档，部署随后会按照与 Web 表单相同的字段定义验证每项资源。

---

## 完整示例

### 📱 本地自行部署配置

```yaml
version: 1

environments:
  - name: local
    display_name: 本地环境
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
    display_name: 开发助手
    model: <model-name>
    environment_name: local
    system: |
      协助处理代码仓库中的任务，并说明每项修改。
    enabled: true
```

### 代码仓库与扩展配置

```yaml
version: 1

agents:
  - name: reviewer
    display_name: 代码审查
    description: 审查应用代码仓库中的变更
    model: <model-name>
    environment_name: default
    system: |
      检查变更的正确性、测试和运维风险。
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

### 生产环境配置

```yaml
version: 1

environments:
  - name: production
    display_name: 生产环境
    description: 网络访问受限的生产 Agent 环境
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
    display_name: 事故复盘
    description: 收集证据并起草事故复盘
    model: <model-name>
    environment_name: production
    system: |
      先收集证据，再给出结论。
      列出使用的每项日志、变更和时间线信息。
    exposure_mode: both
    prewarm_enabled: true
    enabled: true
```

### 🎯 最小配置示例

**最小 Environment**：

```yaml
version: 1
environments:
  - name: default
    engine_kind: <agent-program-id>
```

**最小 Agent**：

```yaml
version: 1
agents:
  - name: assistant
    model: <model-name>
    environment_name: default
```

最小 Agent 引用的 Environment 没有在同一份文件中声明时，需要已经存在于目标部署中。

---

## 常见问题

### ❓ 找不到配置文件

**问题**：命令无法读取 `astrabox.yaml`。

**解决办法**：

```bash
# 创建带注释的骨架
astrabox init

# 或导出现有部署
astrabox init --from-deployment

# 或指定实际文件
astrabox diff --file config/production.astrabox.yaml
```

### ❓ YAML 格式错误

**问题**：缩进、引号或列表格式不正确。

**解决办法**：

1. 使用空格，不使用 Tab。
2. 资源列表中的每一项都以短横线（`-`）开始。
3. YAML 可能识别为其他类型的值应加引号。
4. 运行 `astrabox diff -f astrabox.yaml`；YAML 无效时，命令会在发送请求前以退出码 2 结束。

### ❓ 缺少必填字段

**问题**：资源没有包含必填字段。

**解决办法**：

```bash
# 查看目标部署的必填字段
astrabox schema environment
astrabox schema agent

# 导出当前资源的完整配置再修改
astrabox init --from-deployment --force
```

每项资源都需要 `name`。当前 Agent 还需要 `model` 和 `environment_name`，当前 Environment 需要 `engine_kind`。已安装的 Plugin 扩展字段后，仍以目标部署接受的字段为准。

### ❓ 占位符或环境变量没有被替换

**问题**：`<model-name>` 或 `${MODEL_NAME}` 这样的值被原样发送给部署。

**解决办法**：

应用配置前，请替换文档中的占位符。AstraBox CLI 不会展开 `astrabox.yaml` 中的环境变量或模板表达式。

进程环境变量只用于设置部署地址和 CLI 鉴权凭证：

```bash
export ASTRABOX_ENDPOINT=https://astrabox.example.com
export ASTRABOX_TOKEN=<access-token>
astrabox diff -f astrabox.yaml
```

### ❓ 配置修改没有生效

先查看命令报告的处理结果：

```bash
# 1. 预览文件
astrabox diff -f astrabox.yaml

# 2. 应用文件
astrabox apply -f astrabox.yaml

# 3. 读取已经保存的资源
astrabox get agents <agent-name> --output json
astrabox get environments <environment-name> --output json
```

- `unchanged` 表示文件声明的内容已经一致。
- 从文件中移除资源不会删除它，`apply` 从不执行删除。
- Agent 使用 `environment_name` 指定的 Environment。
- Agent 程序选项只有在所选 Agent 程序支持时才会生效。
- Environment 中的凭证掩码未修改时，会保留已保存的凭证。

### ❓ 字段名不兼容

文档顶层只接受 `version`、`environments` 和 `agents`。资源只接受目标部署声明的字段。

不要为被拒绝的字段改名或添加兼容字段。请导出当前格式，或直接查看字段：

```bash
astrabox init --from-deployment --force
astrabox schema environment --output json
astrabox schema agent --output json
```

---

## 配置字段速查表

### 文档顶层字段

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `version` | ✅ | 必须为 `1`。 |
| `environments` | ❌ | Environment 声明列表。 |
| `agents` | ❌ | Agent 声明列表。 |

### Environment 字段

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `name` | ✅ | Environment 名称和文件中的标识。 |
| `display_name` | ❌ | 向用户显示的名称。 |
| `description` | ❌ | Environment 的用途。 |
| `engine_kind` | ✅ | Agent 程序。 |
| `enabled` | ❌ | Environment 是否可以使用。 |
| `sandbox_backend` | ❌ | 沙箱后端。 |
| `runtime_template_name` | ❌ | 沙箱后端的运行模板。 |
| `networking` | ❌ | 沙箱出站网络规则。 |
| `idle_action` | ❌ | 沙箱空闲后的处理方式。 |
| `sandbox_tenancy` | ❌ | 对话级或 Agent 级沙箱复用。 |
| `sandbox_permission_level` | ❌ | 沙箱内部权限级别。 |
| `endpoint_provider` | ❌ | 模型服务连接类型。 |
| `provider_access` | ❌ | 模型服务地址和凭证引用。 |
| `tracing` | ❌ | Agent 程序的链路追踪输出。 |

### Agent 字段

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `name` | ✅ | Agent 名称和文件中的标识。 |
| `display_name` | ❌ | 向用户显示的名称。 |
| `description` | ❌ | Agent 的用途。 |
| `icon` | ❌ | 向用户显示的图标引用。 |
| `tags` | ❌ | 用于整理 Agent 的标签。 |
| `use_cases` | ❌ | 示例任务。 |
| `model` | ✅ | 模型名称或模型路由名称。 |
| `system` | ❌ | 系统提示词。 |
| `engine_options` | ❌ | Agent 程序定义的设置。 |
| `skills` | ❌ | Agent 可以使用的 Skill。 |
| `mcp_servers` | ❌ | Agent 自己的 MCP Server 配置。 |
| `terminal_panel` | ❌ | 布尔值；是否显示终端面板，不控制命令执行权限。 |
| `diff_panel` | ❌ | 布尔值；是否显示文件差异面板，不控制文件修改权限。 |
| `default_repo` | ❌ | 主代码仓库。 |
| `plugin_repos` | ❌ | 提供 Plugin 的代码仓库。 |
| `environment_name` | ✅ | Agent 使用的 Environment。 |
| `exposure_mode` | ❌ | 对话和 MCP 使用方式。 |
| `idle_hibernate_seconds` | ❌ | 休眠前的空闲时间。 |
| `prewarm_enabled` | ❌ | 为 Agent 保持完整的准备运行时。 |
| `enabled` | ❌ | Agent 是否可以使用。 |

---

## 下一步

- 📖 [CLI 概览](./overview.md) - 了解主要能力和使用方式
- 🎮 [命令详解](./commands.md) - 学习每个命令的用法
- 🚀 [快速开始](../quickstart.md) - 按照端到端流程启动并配置 AstraBox
