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
可展开。生成功能默认开启。模型连接字段留空不会关闭生成：AstraBox 服务端会用服务端的网关凭证，
通过模型网关的服务端地址发送这些请求，并使用部署的默认模型。服务端的网关凭证只会发给网关本身：
`title_model.base_url` 指向其他端点时，必须配置它自己的 `title_model.api_key` 或
`title_model.api_key_secret_name`，否则 AstraBox 拒绝启动。

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

DeepSeek Harness 对其原生 DeepSeek 目录中的模型名称（本版本内置目录中的
`deepseek-flash` 和 `deepseek-v4-pro`）使用 Messages 协议，对其他网关路由名称使用
Chat Completions。网关必须支持所选协议。原生 DeepSeek 图片上传还使用 Files API。
内置网关同时提供这两种协议。对于这些目录模型，外部网关还必须支持原生 Messages 和
Files；只提供 Chat Completions 的端点需要使用其他路由名称。

其他路由名称使用所选网关的 Chat Completions 接口，包括图片输入，不会把图片上传到
DeepSeek 的 Files API。对于 LiteLLM 无法识别的自定义视觉模型，在路由中声明
`model_info.supports_vision: true`。这个设置描述上游模型的实际能力，不会让纯文本模型
获得图片理解能力。

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

需要以代码管理配置时：内置路由定义在 `containers/litellm/config.yaml` 中。要提供自己的
路由，把替换文件挂载到 AstraBox 容器内的 `/opt/astrabox/litellm/config.yaml`：

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

打开**管理台 → Agent**，创建或编辑 Agent。先选择 **RuntimeProfile（运行配置）**。
RuntimeProfile 决定运行哪个 Agent 程序，以及使用哪些沙箱设置、网络访问方式和模型连接。
模型输入框随后会搜索该 RuntimeProfile 的模型连接；可以选择搜索结果，也可以输入准确的路由
名称。

各部分的职责如下：

| 部分 | 职责 |
|---|---|
| Agent | 保存所选模型或路由名称 |
| RuntimeProfile | 选择 AstraBox 准备运行实例时使用的 Agent 程序和模型连接 |
| Agent 程序 | 按照自身原生模型协议组织请求 |
| LiteLLM | 把路由映射到上游模型，并执行网关路由、预算、日志和故障切换 |

使用内置 LiteLLM 连接时，部署设置决定推理网关。RuntimeProfile 中保存的连接信息也用于让
管理台查询该 RuntimeProfile 的模型列表。网关没有返回模型列表时，模型输入框仍然支持直接
填写准确的路由名称。

修改 Agent 的模型或 RuntimeProfile 不会直接重新配置正在运行的实例。AstraBox 会在首次
准备或重建该 Session 运行实例时读取当前设置。

## 向 Agent 程序描述模型 {#model-description}

Agent 程序依据它对模型的了解安排工作：按上下文窗口压缩对话、限制每次回复的长度、
只给能读图的模型发送图片、只提供模型接受的推理档位。网关路由名本身不包含这些信息。
AstraBox 准备运行环境时，会向模型连接查询所选模型的信息，并写入 Agent 程序自己的
模型配置。

| 信息 | LiteLLM `/model_group/info` 字段 | 使用它的 Agent 程序 |
|---|---|---|
| 上下文窗口 | `max_input_tokens` | Claude Code、Codex、Pi、DeepSeek Harness、Hermes |
| 输出上限 | `max_output_tokens` | Claude Code、Pi、DeepSeek Harness |
| 图片输入 | `supports_vision` | Codex、Pi、DeepSeek Harness、Hermes |
| 推理档位 | `supported_reasoning_efforts` | Codex、Pi、DeepSeek Harness |

模型连接没有说明的信息不会写入，由程序使用自己的默认值。AstraBox 不自行判断模型
能力：模型或程序拒绝的请求，以该程序自己的错误失败。程序自带目录中已描述的模型
（例如 DeepSeek Harness 中的 DeepSeek 模型）沿用程序自己的描述。

LiteLLM 依据自带的模型表回答。模型表未描述的路由，在路由的 `model_info` 中声明：

```yaml
model_list:
  - model_name: "company-code-model"
    litellm_params:
      model: "openai/provider-model-id"
      api_base: "https://models.example.com/v1"
      api_key: os.environ/COMPANY_MODEL_API_KEY
    model_info:
      max_input_tokens: 200000
      max_output_tokens: 32000
      supports_vision: true
      supported_reasoning_efforts: ["low", "medium", "high"]
      supports_low_reasoning_effort: true
```

对未描述的模型，LiteLLM 报告 `supports_vision` 为 false，因此这类路由会被描述为
仅支持文本。`supported_reasoning_efforts` 列出路由接受的档位，LiteLLM 会把请求的
档位取整到列表中最接近的一档。同时声明逐档标志（`supports_none_reasoning_effort`、
`supports_minimal_reasoning_effort`、`supports_low_reasoning_effort`、
`supports_xhigh_reasoning_effort`、`supports_max_reasoning_effort`）：LiteLLM 在
Messages 协议上转发档位前会读取它们。内置配置在 DeepSeek 路由上声明了 DeepSeek
接受的档位。

LiteLLM 以外的模型连接，通过其模型端点 Provider（entry-point 组
`astrabox.providers.model`）的 `describe_model` 方法说明这些信息；未实现该方法的
Provider 不说明任何信息，各程序使用自己的默认值。AstraBox 每次准备运行环境时都会
重新读取这些信息。

## 让模型凭证留在 Agent 程序之外

内置网关保存上游模型服务密钥。启用凭证保护后，Agent 程序拿到的是网关凭证占位符；只有
匹配的模型请求经过沙箱服务时，才会添加真实凭证。Agent 不保存模型密钥，RuntimeProfile
编辑页面也只会返回掩码，不会返回已保存的原始值。

具体的请求匹配过程见[保护 Agent 使用的凭证](egress-credential-injection.md)。

## 配置对话标题请求

自动标题由 AstraBox 服务端单独发起一次非流式模型请求。标题、标题判定和执行过程摘要都会
明确发送 `reasoning_effort: "none"`，输出上限为 2048 个 token。这些请求不会继承 Agent 的推理
设置。思考是否真正关闭，取决于 LiteLLM 用哪条路由提供标题模型。安装脚本配置的路由如下：

| 内置网关上的标题路由 | LiteLLM 发往上游的内容 |
|---|---|
| `deepseek/<model>`、`deepseek-flash` 和 `deepseek-v4-pro`（LiteLLM 原生 DeepSeek 适配器） | `thinking: {"type": "disabled"}` |
| `claude-*` 和 `anthropic/<model>`（Anthropic Messages） | 不带 `thinking` 字段，即 Anthropic 默认不启用扩展思考。默认会思考的 Anthropic 兼容服务仍会思考。 |
| `openai-compatible/<model>` | 不发送：对于它不认识的模型，LiteLLM 会丢弃这个选项，由服务自身的默认值决定 |

使用 DeepSeek 的端点时，AstraBox 会自动为标题选择 `deepseek/<model>`。其他服务的默认模型
如果会思考，请把 `title_model.model_name`（`ASTRABOX_TITLE_MODEL_NAME`）设为不思考的路由。
不思考的路由写完标题就会停止，不会用满这个上限；会思考的路由则需要把推理也装进上限：
用完上限的回复没有正文，会话会保留默认标题。在 DeepSeek 会思考的路由上，2048 个 token
足够生成标题和执行过程摘要；不思考的路由只会用到其中一小部分，响应也更快。

标题使用用户的语言。由于以公式为主的消息几乎没有可供模型判断的文字，请求会写明用户消息
所用的书写系统。标题如果使用了用户没有使用过的非拉丁书写系统，会被记为标题生成失败，而不会
显示出来；名称和代码使用的拉丁字母始终允许。

在 `astrabox/config/app.yml` 中配置请求超时时间：

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
读取、写入和连接池等待，并不是整个请求的截止时间，也不会增加重试。超时后会保留
默认的会话标题，并在管理员会话详情和服务端日志中记录超时类型和配置的时长。

## 使用 ChatGPT 套餐 {#use-a-chatgpt-plan}

用户可以通过 OpenAI 的 [Sign in with ChatGPT](https://developers.openai.com/siwc/token-sharing-open-source)
套餐用量，把一个或多个 ChatGPT 账号关联到自己的 AstraBox 用户。AstraBox 服务端把每个账号的
访问令牌、刷新令牌和 ID 令牌保存在它的密钥存储中，并自行续期访问令牌；这些令牌从不到达
用户的电脑。

在用浏览器登录的那台电脑上关联账号：

```bash
astrabox model-account login chatgpt
```

OpenAI 会把浏览器送回 `http://127.0.0.1:<port>/auth/callback`，这是那台电脑上的地址，因此
命令在打开 OpenAI 的登录页面之前，先在一个空闲端口上监听这个地址。随后它把浏览器带回的结果
交给部署，部署兑换授权码、验证 ID 令牌并保存账号。请在 OpenAI 的页面上允许使用套餐用量：
没有允许的登录不会关联任何账号。AstraBox 服务端必须能访问 `https://auth.openai.com`，
列出账号的模型时还需要访问 `https://api.openai.com`。

管理台的**模型账号**页面（在**配置**下）列出你已关联的账号及其状态，可以重命名账号、选择
默认账号和移除账号。页面显示指向本部署的 `login` 命令，供你复制到自己的电脑上运行；由于
OpenAI 会把浏览器送回 `127.0.0.1`，页面本身无法关联账号。

- **多个账号。** 每次通过 `login chatgpt` 登录关联一个账号。第一个账号成为用户的默认
  ChatGPT 账号。除非部署开启了[团队账号池](#share-accounts-with-the-team)，没有任何机制会
  自行在账号之间切换。在模型账号页面上，或用 `PATCH /api/v1/me/model-accounts/{account_id}`
  加 `{"is_default": true}`，把另一个账号设为默认。用户在每个厂商下各有一个默认账号，因此
  默认 ChatGPT 账号和默认 Anthropic API key 可以并存。删除默认账号后，用户在该厂商下就没有
  默认账号了。
- **状态。** `astrabox model-account list` 显示每个账号的状态。OpenAI 永久拒绝续期的账号
  （刷新令牌已过期、已撤销或被重复使用）状态为 `sign_in_required`；用
  `astrabox model-account login chatgpt --account <account_id>` 重新登录它。
- **移除。** `DELETE /api/v1/me/model-accounts/{account_id}` 先在 OpenAI 撤销该账号的会话，
  再删除账号及其令牌。OpenAI 没有确认撤销时，响应中的 `revocation_confirmed` 为 `false`；
  请在 ChatGPT 的设置中断开 AstraBox 来结束该会话。

### 在它上面运行 Codex 对话

`endpoint_provider` 为 `chatgpt_plan` 的 RuntimeProfile，以对话所有者关联的 ChatGPT 账号
把其对话的模型请求发送到 OpenAI 的 API，OpenAI 计入该账号的套餐。请求不经过模型网关，
因此网关不会为这些对话记录花费。

- 该 RuntimeProfile 的 `engine_kind` 必须是 `codex`，`sandbox_tenancy` 必须是
  `conversation`，并且部署必须保持受保护凭证传递开启。使用它的 Agent 的
  `prepared_capacity` 必须为 0：对话出现之前准备好的沙箱没有所有者，也就没有可用的账号。
- Agent 的模型是该账号可以使用的模型，例如 `gpt-6.1-sol`。在这样的 RuntimeProfile 上，
  Agent 表单提供所选账号的模型（通过 `GET /api/v1/me/model-accounts/{account_id}/models`
  从 OpenAI 读取，`default?vendor=chatgpt_plan` 指你的默认 ChatGPT 账号），并在模型旁边提供
  ChatGPT 账号字段。这两者在其他 RuntimeProfile 上都不会显示，表单在那里也不会发送
  `model_account_id`。
- 由对话所有者付费。没有关联账号的人发起的对话以 `MODEL_ACCOUNT_REQUIRED` 结束。Agent 可以
  在 `model_account_id` 中指定其创建者的某个账号；此后创建者与它的对话使用该账号而不是创建者
  的默认账号，其他人的对话使用他们自己的默认账号。Session 详情
  （`GET /api/v1/sessions/{session_id}`）在 `model_account_id` 中给出其最近一次回复消耗了
  哪个账号的套餐。
- 沙箱中只有占位符。出站边界只在 `POST https://api.openai.com/v1/responses` 上附加账号的
  访问令牌，服务端在每轮开始前以及回复运行期间续期该令牌。
- 套餐用量达到上限时，回复失败，不会被重放。它的 `error` 保留 Codex 的报告（`message`、
  `codexErrorInfo`），并加上 `code` `MODEL_PLAN_USAGE_LIMIT`、达到上限的 `account_id` 和
  `resets_at`。`resets_at` 始终为 `null`：OpenAI 的套餐用量错误不说明上限何时重置，OpenAI
  也要求应用不要自行推断。不会切换到网关。没有团队账号池时，下一次回复仍消耗同一个账号。
  OpenAI 没有提供读取剩余用量的方式；账号所有者可以在
  [ChatGPT Settings → Usage](https://chatgpt.com/settings/usage) 中查看。
- 对话标题仍然通过部署的模型网关生成，而不是使用用户的套餐。

OpenAI 为开源和自托管应用提供了套餐用量的文档，并要求付费服务或远程托管服务的运营者先
[登记意向](https://openai.com/form/sign-in-with-chatgpt-interest/)。

### 与团队共享账号 {#share-accounts-with-the-team}

团队成员可以互相使用彼此的 ChatGPT 套餐：某位成员的账号达到套餐用量上限时，他们的对话会
继续使用另一位成员选择共享的账号。

管理员用 `ASTRABOX_MODEL_ACCOUNT_POOL_ENABLED=true`（默认 `false`；重启后生效）为部署开启
账号池。之后由每个账号的所有者决定是否共享：模型账号页面上的**与团队共享**开关、
`astrabox model-account share <account_id>`（以及 `unshare`），或
`PATCH /api/v1/me/model-accounts/{account_id}` 加 `{"shared": true}`。所有者表态之前，任何
账号都不会被共享。`GET /api/v1/me/model-accounts/team-pool` 回答账号池是否开启；账号池关闭
时，共享以 `MODEL_ACCOUNT_POOL_DISABLED` 被拒绝，对话的行为如上文所述。账号池只容纳 ChatGPT
账号：共享 [Anthropic API key](#anthropic-api-key) 会以 `MODEL_ACCOUNT_NOT_SHAREABLE` 被
拒绝，对话也只会得到其 RuntimeProfile 所消耗类型的账号。

账号池开启时，对话在每次回复之前按以下顺序选择要消耗的账号：

1. 所有者的首选账号（其 Agent 指定的账号，或默认 ChatGPT 账号），除非用量上限让该账号
   暂停使用；
2. 否则，所有者的其他可用 ChatGPT 账号，默认账号优先；
3. 否则，同一组织的其他成员共享的可用 ChatGPT 账号，最早关联的优先。

OpenAI 报告某个账号达到套餐用量上限后，该账号暂停使用一小时，之后再次提供；所有者账号的
这一小时一过，对话就会回到该账号。达到上限的那次回复保持失败，不会被重放。回复运行期间不会
更换账号：回复运行时发送的输入、回复期间访问令牌的续期，以及重新连接到正在运行的沙箱，都
沿用该回复开始时的账号。所有候选账号都在暂停时，输入以 `MODEL_PLAN_USAGE_LIMIT` 被拒绝，
其 `evidence` 给出所有者的账号以及它暂停到何时。没有任何关联账号的成员不会得到共享账号；
他们的对话以 `MODEL_ACCOUNT_REQUIRED` 结束。

所有者可以在模型账号页面和 `GET /api/v1/me/model-accounts` 中看到自己账号的状态：`shared`、
`pool_last_used_at`（账号池最近一次把它交给队友的对话用于一次回复的时间）和
`usage_limited_until`（已报告的上限让它暂停使用到何时）。成员只能看到其对话所消耗的共享
账号的 `account_id`，看不到所有者的姓名、邮箱或令牌；与任何账号一样，令牌保留在服务端。
停止共享从每个对话的下一次回复起生效；已经在运行的回复保留它已有的访问令牌，此后该令牌
不再续期。

共享消耗的是每位成员自己的登录，并且只在账号所有者选择加入之后。运营者要为其成员共享的
ChatGPT 套餐的条款负责。

AstraBox 从 Codex 对失败轮次的报告中读取用量上限。Codex 0.171 不会传递 OpenAI 的代码
`subscription_sharing_usage_limit_exceeded`：对于来自模型 API、它不认识的任何 HTTP 429，它
都报告为 `codexErrorInfo` `{"responseTooManyFailedAttempts": {"httpStatusCode": 429}}`。
AstraBox 把这种报告以及 Codex 自己的 `usageLimitExceeded` 都算作套餐用量上限。在 ChatGPT
套餐 RuntimeProfile 上，OpenAI 只为套餐用量上限记录了 HTTP 429；它发送的任何其他 429 也会
让账号暂停使用一小时。

## 使用你自己的 Anthropic API key {#anthropic-api-key}

用户可以把自己的 Anthropic API key 保存为其模型账号之一。在 `endpoint_provider` 为
`anthropic_api_key` 的 RuntimeProfile 上，Claude Code 对话用对话所有者的 key 把模型请求
发送到 Anthropic 的 API，Anthropic 向该 key 所属的组织计费。请求不经过模型网关，因此网关
不会为这些对话记录花费。Anthropic 关于在托管产品中使用 Claude Code 的条款要求每位最终用户
用自己的 key 或套餐认证；这里提供的是 API key 方式。

在管理台的**模型账号**页面上、用 `astrabox model-account add anthropic`，或用以下请求添加
key：

```http
POST /api/v1/me/model-accounts/anthropic
Content-Type: application/json

{"api_key": "sk-ant-…", "label": "Personal key"}
```

AstraBox 服务端用这个 key 读取 Anthropic 的模型列表来检查它
（`GET https://api.anthropic.com/v1/models`，key 放在 `x-api-key` 中），只有 Anthropic 接受
时才保存；被拒绝的 key 以 `MODEL_ACCOUNT_API_KEY_REJECTED` 失败，不保存任何内容。服务端必须
能访问 `https://api.anthropic.com`。key 保存在密钥存储中，之后任何响应或页面都不会再显示它。
请使用属于某个 Anthropic workspace 的 key：对于不限定在某个 workspace 的 key，Anthropic 要求
请求带 `anthropic-workspace-id` 请求头，而 AstraBox 不发送这个请求头。

- **多个 key。** 用户可以保存多个 key；第一个是其默认 Anthropic API key，与默认 ChatGPT 账号
  相互独立。key 不能共享到[团队账号池](#share-accounts-with-the-team)：对话只消耗其所有者的
  key，不消耗其他任何人的。
- **替换 key**：用 `PATCH /api/v1/me/model-accounts/{account_id}` 加 `{"api_key": "…"}`，或用
  `astrabox model-account add anthropic --account <account_id>`。新 key 以同样的方式检查。
  正在运行的对话从下一次回复起发送新 key。
- **移除 key**：用 `DELETE /api/v1/me/model-accounts/{account_id}`，或在模型账号页面上移除。
  AstraBox 删除它保存的副本；它无法在 Anthropic 撤销这个 key，因此需要让 key 在所有地方失效
  时，请在 Claude Console 中撤销它。

### 在它上面运行 Claude Code 对话

- 该 RuntimeProfile 的 `engine_kind` 必须是 `claude_code`，`sandbox_tenancy` 必须是
  `conversation`，并且部署必须保持受保护凭证传递开启。使用它的 Agent 的
  `prepared_capacity` 必须为 0。
- Agent 的模型是该 key 可以使用的模型，例如 `claude-sonnet-4-5`。在这样的 RuntimeProfile
  上，Agent 表单提供所选 key 的模型（通过 `GET /api/v1/me/model-accounts/{account_id}/models`
  从 Anthropic 读取，`default?vendor=anthropic_api_key` 指你的默认 key），并在模型旁边提供
  Anthropic API key 字段。
- 由对话所有者付费。没有 key 的人发起的对话以 `MODEL_ACCOUNT_REQUIRED` 结束。Agent 可以在
  `model_account_id` 中指定其创建者的某个 key；此后创建者与它的对话使用该 key，其他人的对话
  使用他们自己的默认 key。
- 沙箱在 `ANTHROPIC_API_KEY` 中只有占位符，Claude Code 与它自己的默认端点
  `https://api.anthropic.com` 通信，不设置 `ANTHROPIC_BASE_URL`。出站边界只在发往
  `api.anthropic.com` 的 `/v1/` 下的 `GET` 和 `POST` 请求中用 key 替换占位符，其他地方都不
  替换。Claude Code 运行的命令会继承这个占位符，因此它们向 Anthropic API 发出的请求同样会
  消耗这个 key。
- key 达到其速率或花费上限时，回复以 Anthropic 的错误失败；不会切换到其他 key 或网关。
- 对话标题仍然通过部署的模型网关生成。

## 验证连接

开放 Agent 给用户使用前：

- 分别运行一次普通回复和一次工具或 MCP 调用；
- 检查无效密钥、限流和超时产生的错误；
- 在 LiteLLM 中确认用量与费用归属；
- 确认 RuntimeProfile 的网络规则允许访问网关地址。

## 相关指南

- [定义 Agent](authoring-agents.md)
- [RuntimeProfile](runtime-profiles.md)
- [部署 AstraBox](deploy.md)
- [保护 Agent 使用的凭证](egress-credential-injection.md)
