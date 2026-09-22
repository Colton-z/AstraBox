# 连接模型服务

AstraBox 内置的模型连接会把每次模型请求发送到 LiteLLM。Agent 使用稳定的模型名称；上游
地址、凭证、路由、预算和请求日志由 LiteLLM 保存在服务端。部署也可以安装其他模型连接；
这里介绍 AstraBox 自带的 LiteLLM 连接。

![Agent 如何访问模型服务](./img/models-request-path.svg#inline)

## 可选的标题与过程摘要

自动生成的对话标题和执行过程摘要是可选功能。要同时关闭两者而不影响 Agent 对话，在部署
YAML 中设置以下内容并重启 AstraBox：

```yaml
astrabox:
  title_model:
    enabled: false
```

对应的环境变量是 `ASTRABOX_TITLE_MODEL_ENABLED=false`。已保存的标题和摘要仍可正常查看。
新的执行记录使用固定的外层“过程”标题，以及内层的“工具调用”或“推理”标题；工具记录仍
可展开。生成功能默认开启。模型连接字段留空时会复用主模型配置，不会关闭生成。

## 选择适合部署方式的连接

| 方案 | 适用场景 | 需要配置什么 |
|---|---|---|
| 内置 LiteLLM | 在一台主机上安装 AstraBox，或先进行试用 | 模型服务凭证和一条 LiteLLM 路由 |
| 已有 LiteLLM | 组织已经运行统一模型网关 | 沙箱可访问的地址、权限受限的推理密钥，以及可选的服务端访问地址 |
| 通过 LiteLLM 使用本地模型 | 模型运行在工作站或内网 | 一条指向 AstraBox 主机可访问地址的 LiteLLM 路由 |

无论采用哪种 LiteLLM 方案，Agent 选择的都是 `company-code-model` 这样的**路由名称**。
LiteLLM 会把这个名称映射到实际模型服务。以后更换上游模型时，可以保留路由名称，使用它
的 Agent 无须逐个修改。

## 一次模型请求如何运行

1. Agent 程序按照它支持的协议发送请求。
2. 沙箱访问当前 AstraBox 部署配置的 LiteLLM 地址。
3. LiteLLM 根据 Agent 选择的路由名称找到上游模型，添加上游凭证并发起调用。
4. LiteLLM 把结果流式返回，同时记录已配置的用量数据。

Agent 程序只会得到请求所需的模型名称和网关访问方式。上游模型服务密钥保存在 LiteLLM
一侧。

## 使用内置网关

标准 AstraBox 容器会在运行 AstraBox 服务的同时启动 LiteLLM。先为内置配置中的路由提供
凭证和模型：

```bash
export ANTHROPIC_API_KEY="your-anthropic-api-key"
export ANTHROPIC_MODEL="your-model-name"
scripts/compose.sh up --build -d
```

打开**管理台 → 集成服务 → LiteLLM 网关**，可以管理模型路由、服务商凭证、密钥、预算、
费用和请求日志。内置网关直接使用 AstraBox 管理员会话，不需要再次登录 LiteLLM。

需要把配置纳入版本控制时，可以编辑 `containers/litellm/config.yaml`，或把自己的配置文件
挂载到 AstraBox 容器内的 `/opt/astrabox/litellm/config.yaml`：

```yaml
model_list:
  - model_name: "company-code-model"
    litellm_params:
      model: "openai/provider-model-id"
      api_base: "https://models.example.com/v1"
      api_key: os.environ/COMPANY_MODEL_API_KEY
```

在 AstraBox 服务上设置 `COMPANY_MODEL_API_KEY`，重启部署，再在 Agent 表单中使用
`company-code-model`。路由指向明确的上游模型版本后，模型变更由路由管理员统一控制。

## 连接已有 LiteLLM 网关

设置沙箱能够访问的地址，以及权限只覆盖推理接口的密钥：

```bash
ASTRABOX_LITELLM_BASE_URL=https://llm-gateway.example.com
ASTRABOX_LITELLM_API_KEY=sk-scoped-inference-key
```

`ASTRABOX_LITELLM_BASE_URL` 用于 Agent 的模型请求。沙箱网络需要能够解析并访问其中的
主机名。设置该地址后，AstraBox 不会再启动内置 LiteLLM 进程。

有些部署让沙箱通过私有 DNS 访问网关，而 AstraBox 服务通过另一个服务网络地址访问同一
网关。此时可以为 Agent 编辑页面的模型搜索配置第二个地址：

```bash
ASTRABOX_LITELLM_SERVER_BASE_URL=http://litellm.internal
```

如需在**集成服务**中显示外部网关的浏览器管理页面，再设置公开管理地址：

```bash
ASTRABOX_LITELLM_ADMIN_URL=https://llm-admin.example.com/ui/
```

团队部署配置 `containers/compose.team-gateway.yaml` 要求沙箱访问的网关使用 HTTPS、完整
域名和 443 端口，并要求显式提供权限受限的推理密钥。

## 连接本地模型

先通过 LiteLLM 暴露本地模型，再为它设置稳定路由。以下示例连接一项可从 AstraBox 主机
访问的 Ollama 服务：

```yaml
model_list:
  - model_name: "local-code-model"
    litellm_params:
      model: "ollama_chat/qwen3:32b"
      api_base: "http://host.docker.internal:11434"
```

这个地址由 LiteLLM 所在网络解析。在 Linux 或远程 Docker 主机上，应把
`host.docker.internal` 换成 AstraBox 容器能够解析的地址。网络检查方法见
[部署 AstraBox](deploy.md)。

所选模型和 LiteLLM 路由需要兼容 Agent 程序要求的请求协议与工具调用方式。

## 为 Agent 选择模型

打开**管理台 → Agent**，创建或编辑 Agent。先选择 **Environment（运行环境）**。
Environment 决定运行哪个 Agent 程序，以及使用哪些沙箱设置、网络访问方式和模型连接。
模型输入框随后会搜索该 Environment 的模型连接；可以选择搜索结果，也可以输入准确的路由
名称。

各部分的职责如下：

| 部分 | 职责 |
|---|---|
| Agent | 保存所选模型或路由名称 |
| Environment | 选择 AstraBox 准备运行实例时使用的 Agent 程序和模型连接 |
| Agent 程序 | 按照自身原生模型协议组织请求 |
| LiteLLM | 把路由映射到上游模型，并执行网关路由、预算、日志和故障切换 |

使用内置 LiteLLM 连接时，部署设置决定推理网关。Environment 中保存的连接信息也用于让
管理台查询该 Environment 的模型列表。网关没有返回模型列表时，模型输入框仍然支持直接
填写准确的路由名称。

修改 Agent 的模型或 Environment 不会直接重新配置正在运行的实例。AstraBox 会在首次
准备或重建该 Session 运行实例时读取当前设置。

## 让模型凭证留在 Agent 程序之外

内置网关保存上游模型服务密钥。启用凭证保护后，Agent 程序拿到的是网关凭证占位符；只有
匹配的模型请求经过沙箱服务时，才会添加真实凭证。Agent 不保存模型密钥，Environment
编辑页面也只会返回掩码，不会返回已保存的原始值。

具体的请求匹配过程见[保护 Agent 使用的凭证](egress-credential-injection.md)。

## 配置对话标题请求

自动标题由 AstraBox 服务端单独发起一次非流式模型请求。标题、标题判定和执行过程摘要都会
明确发送 `reasoning_effort: "none"`，因此需要配置支持非思考模式补全的模型端点；LiteLLM
会为所选服务商转换这个选项。这些请求不会继承 Agent 的推理设置。在
`astrabox/config/app.yml` 中配置它们的超时时间：

```yaml
astrabox:
  title_model:
    request_timeout_seconds: 60
```

默认值为 60 秒。容器部署时，在 Compose YAML 的 `services.server.environment` 中设置等效值：

```yaml
services:
  server:
    environment:
      ASTRABOX_TITLE_MODEL_REQUEST_TIMEOUT_SECONDS: "60"
```

环境变量优先于应用 YAML。修改应用 YAML 后需要重启应用；修改 Compose 环境变量后需要重建
server 容器。超时时间必须是有限的正数，无效值会导致设置校验失败。

它使用 [HTTPX 的网络超时](https://www.python-httpx.org/advanced/timeouts/)，覆盖连接、
读取、写入和连接池等待，并不是整个 Agent 轮次的截止时间，也不会增加重试。超时后会保留
默认的会话标题，并在管理员会话详情和服务端日志中记录超时类型和配置的时长。

## 验证连接

开放 Agent 给用户使用前：

- 分别运行一次普通回复和一次工具或 MCP 调用；
- 检查无效密钥、限流和超时产生的错误；
- 在 LiteLLM 中确认用量与费用归属；
- 确认 Environment 的网络规则允许访问网关地址。

## 相关指南

- [定义 Agent](authoring-agents.md)
- [Environment](environments.md)
- [部署 AstraBox](deploy.md)
- [保护 Agent 使用的凭证](egress-credential-injection.md)
