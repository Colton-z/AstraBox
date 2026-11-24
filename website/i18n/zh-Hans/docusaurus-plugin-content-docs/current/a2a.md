# Agent 间通信

Assistant 使用 Hermes 自带的 A2A 工具发现并调用其他 Agent。AstraBox 把 Assistant
所有者能够访问的 Agent 作为对端（peer）提供给它，所用凭证限定在该 Assistant 的工作区
和每个目标之内。目标在一个普通的 AstraBox Session 中执行，这个 Session 归调用方所有，
并使用目标配置的引擎。

Hermes 固定使用 0.23.3 版本，提交
`caafe5389ae585ef77387a962aee05b1bcf17029`。它的
[A2A 插件](https://github.com/NousResearch/hermes-agent/blob/caafe5389ae585ef77387a962aee05b1bcf17029/website/docs/user-guide/messaging/a2a.md)
提供 `a2a_discover`、`a2a_call`、`a2a_list`、`a2a_history` 和
`a2a_orchestrate`。Assistant 的 Hermes 网关只服务 Assistant 自己的对话：它的所有消息平台都
不会启用，包括入站 A2A 平台，即使某个平台的凭证在 RuntimeProfile 的变量中也是如此；它的
托管配置拒绝 `platforms` 和 `gateway` 键。AstraBox 管理的 Agent 在 AstraBox Session 中
执行，而不是在这个网关中执行。

对于交给 AstraBox 管理的 Agent 的长任务，加载内置的 `astrabox-agent-task` Skill。
它通过现有 A2A API 设置 `returnImmediately=true` 提交任务，保留任务引用，并使用 Hermes
后台终端进程订阅该任务，接收原生完成通知。Assistant 通过 `get_agent_task` 读取结果，
并在原对话中汇报。观察连接中断不会使任务失败，也不会重新提交任务。
需要在当前回复结束后继续汇报时，必须启用后台通知；否则 Assistant 需要使用原生进程工具等待。
运行环境丢失后可以凭已保存的任务引用重新查询，但观察进程本身不会跨沙箱丢失保留。
同步的原生 `a2a_call` 仍受所配置的对端超时限制。

## 调用 AstraBox 管理的 Agent

使用 Hermes 原生工具时，先调用 `a2a_list`，将完整的 peer key 原样传给
`a2a_call.agent`，例如 `astrabox/<agent_id>`。裸 Agent ID 不是 peer key。
配置名会选择已有凭据；发现地址不能替代调用名。直接传入 HTTP(S) URL 是独立的
无认证调用。Agent Card 获取失败时，调用在发送任务前停止；认证错误不会触发向
基础地址的 POST 请求。

经过认证的外部客户端从
`/api/v1/agents/{agent_id}/.well-known/agent-card.json` 获取 Agent Card，并使用其中
公布的 JSON-RPC URL。这些路由使用部署配置的 Web 身份认证。
服务端传输层使用官方 Python `a2a-sdk` 1.2.2。
协议绑定为 A2A 1.0 JSON-RPC 加 SSE。RPC 客户端发送
`A2A-Version: 1.0`；补丁版本号会被忽略，其他主版本或次版本会被拒绝。不提供 REST 和
gRPC 绑定。

Assistant 的运行时使用对应的 `/api/v1/assistant-a2a/` 能力令牌（capability）路由。这些路由的 bearer
令牌只授权一个工作区和一个目标，不是通用的 API 凭证。接收方根据平台记录解析当前的
Assistant 所有者，并在每次操作时检查对目标的访问权限。请求内容无法选择其他员工，也无法
取得 Agent 所有者的身份。

A2A 上下文（context）在调用方、组织和目标 Agent 的范围内标识一段对话。新的上下文会
创建一个平台 Session；之后在该上下文中发送的消息继续这个 Session。一个 Task 对应该
Session 中的一条消息，它的状态就是回答这条消息的回复的状态，以 Agent 运行时的报告为准。
Task 元数据包含平台 Session id 和这条消息的输入 id，文本结果中包含该 Session 的控制台
链接。

重复发送相同的 `messageId` 会复用对应的输入；以不同的消息内容复用它会被拒绝。Hermes
会自行生成消息 id，因此让 Assistant 再调用一次是一个新请求，而不是幂等重试。

新任务的消息可以在 `referenceTaskIds` 中引用之前的任务，最多十个，每个都必须是同一调用方
发给它仍可使用的任意 Agent 的任务；其他 id 会以 `InvalidParamsError` 被拒绝。Agent 会在
消息下方收到每个被引用任务的当前情况：它的 id、Agent、状态、状态消息、结果文本的开头，
以及[任务结果扩展](#task-outcome-v1)为它提供的代码引用。回答某个任务交互的消息不能带引用。

## 任务结果 {#task-outcomes}

由 A2A 上下文创建的对话多一个平台工具，位于 `a2a` MCP 服务上：`report_outcome`。无论使用
哪种引擎（Claude Code、Codex、Pi 或 DeepSeek Harness），Agent 都在最终回复之前调用它，说明
请求的结果：

| `state` | 含义 | 任务 |
|---|---|---|
| `TASK_STATE_COMPLETED` | 它完成了所要求的事；`message` 说明它做了什么 | `TASK_STATE_COMPLETED` |
| `TASK_STATE_REJECTED` | 它没有做，也不会做；`message` 说明原因 | `TASK_STATE_REJECTED` |

使用 `TASK_STATE_REJECTED` 时，它可以加上 `handback: true`：请求属于另一个 agent，或必须
拆分，`message` 说明应当改为怎样处理。工作在 git 仓库中时，它给出 `repository`、`branch`、
它推送的最后一个 `commit`，以及它创建的 `pull_request`。平台把这次调用与回复一起记录；回复
以 `completed` 结束时，该回复的最后一次报告决定任务的状态，报告的 `message` 成为任务的
`TaskStatus.message`，位于对话链接之前。以失败或取消结束的回复保持该状态。没有报告的回复
是一个已完成的任务，与没有该工具的对话相同。

任务的状态和状态消息属于 A2A 1.0 核心：任何客户端都会把被拒绝的请求读作
`TASK_STATE_REJECTED` 及其原因。交回和代码引用在核心中没有对应元素；它们通过下文的任务结果
扩展传递，供激活该扩展的客户端使用。

这个工具只存在于 A2A 上下文创建的对话中，`send_agent_message` 送达的控制台对话中没有它。
由于已准备好的 Agent 运行时在任何对话认领它之前就已确定其平台工具，A2A 上下文创建的对话会
启动自己的运行时，而不是认领一个已准备好的运行时。

## 任务结果扩展（v1） {#task-outcome-v1}

URI：`https://www.astrabox.ai/docs/a2a#task-outcome-v1`

这是 [A2A 扩展指南](https://a2a-protocol.org/v1.0.0/topics/extensions/)所说的 profile 扩展：
它向核心对象添加数据，不改变任何方法。Agent Card 在 `capabilities.extensions` 中列出它，并
标明 `required: false`。客户端在每个请求上用 `A2A-Extensions` 请求头激活它，例如
`A2A-Extensions: https://www.astrabox.ai/docs/a2a#task-outcome-v1`；此时响应的
`A2A-Extensions` 响应头会列出它，响应携带的每个任务都带有下面的数据。没有激活时，任务不带
这些数据。

数据放在核心对象的 `metadata` 中，以扩展的 URI 为键，对象的 `extensions` 列表列出该 URI：

- 在 `TASK_STATE_REJECTED` 任务的 `status.message` 上，当 Agent 交回请求时：
  `{"handback": true}`。请求属于另一个 agent，或必须拆分；状态消息说明应当改为怎样处理。
  不带它的被拒绝任务是单纯的拒绝。
- 在任务的结果 artifact（`artifactId` 为 `<task id>:result`）上，当 Agent 给出它们时：
  `repository`、`branch`、`commit`（小写十六进制）和 `pull_request`（http(s) URL），每个都是
  字符串，且都可选。

```json
{
  "status": {
    "state": "TASK_STATE_REJECTED",
    "message": {
      "role": "ROLE_AGENT",
      "parts": [{"text": "Agent declined the task.\n\nThis is a billing change; the billing agent owns it.\n\n[View Agent session](/sessions/…)"}],
      "extensions": ["https://www.astrabox.ai/docs/a2a#task-outcome-v1"],
      "metadata": {"https://www.astrabox.ai/docs/a2a#task-outcome-v1": {"handback": true}}
    }
  }
}
```

## 向已有对话发送消息

Agent 管理员在 Agent 的管理页面上配置 **Agent 协作**：**关闭**、**全部可访问的 Agent** 或
**指定 Agent**。指定 Agent 时按名称选择。这个范围以数据库中的 Agent 配置为准；对话不保存
单独的副本。启用这些工具后，在新的运行时加载 Agent 配置时生效。

内置的 `agents` MCP 服务向已启用的 Agent 对话和 Assistant 提供以下工具：

- `list_agent_conversations` 搜索 Agent 名称和当前的对话标题，包括 AI 生成的标题。它返回
  Session ID、A2A 上下文 ID 和控制台链接。用 `next_cursor` 继续翻页。名称和标题可能重复或
  变化；请按返回的 ID 路由。
- `send_agent_message` 向已有的 `agent_id` 和 `session_id` 发送消息，相同的重试使用稳定的
  `message_id`。它返回一个 A2A Task；结果通过 `get_agent_task` 读取。它不会创建新的对话。

发现和发送消息仅限于调用方自己在这个 Environment 中的 Agent 对话。Agent 配置的范围不会授予
访问其他用户历史的权限，也不会绕过目标 Agent 现有的访问控制。渠道对话不会获得这些个人工具。

消息包含平台解析出的发送方身份和对话链接，并标明为来自另一段对话的消息，而不是用户的直接
指令。目标继续其原生 Session 上下文。忙碌时的输入接纳、调度和回复边界仍由目标引擎决定；
AstraBox 不额外增加消息队列。Task 处于待处理状态并不表示目标已经回答。

## 交互支持范围 {#interaction-boundaries}

固定版本的 Hermes 工具集发送文本，并返回文本、上下文和状态。它不提供独立的任务取消、
订阅或结构化审批工具。不能仅因为这些操作是 A2A 协议方法，就认为它们可用。AstraBox
在 Assistant 已签名的 `agents` MCP 服务中提供 `get_agent_task(agent_id, task_id)` 和
`answer_agent_task(agent_id, task_id, interaction_id, answer, message_id)`，补充原生的
文本分派。待处理状态的文本包含明确的 Task id、交互 id 和声明的决策选项。先读取 Task
公开的交互约定，取得用户明确的回答或授权，再针对这些确切的 id 提交该回答。保留声明的
决策及其适用范围：Codex 可能提供 `accept` 或 `acceptForSession`，而不是通用的
`approve`。只有完全相同的重试才复用 `message_id`。回答工具返回当前 Task，不等待执行
结束；后续进展通过 `get_agent_task` 读取。

这两个工具与原生对端调用同一个 A2A 服务，每次调用都根据工作区绑定关系解析当前的
Assistant 所有者。Task 访问权限、目标访问权限和交互是否有效，同样由该服务检查。返回
结果的公开结构相同。不需要替换 Hermes 插件，也不需要另一条 Agent 执行路径：固定版本的
纯文本工具无法表达的结构化操作，由 MCP 桥接提供。

`CancelTask` 停止某个任务的消息所启动的工作：

- 还没有运行时收到的消息会被撤回。任务显示为 `TASK_STATE_CANCELED`，对话把这条消息显示为
  已被其发送方取消。
- 正在运行的回复所回答的消息，随该回复一起停止，该回复以 `cancelled` 结束。
- 运行时排在其他工作之后的消息，会与正在进行的回复以及其他所有排队内容一起取消，因为运行时
  只能一次性丢弃全部排队消息。Codex 和 Pi 可以丢弃它们。Claude Code、DeepSeek Harness 和
  Hermes 会在前面的工作结束后运行排队的消息，因此这样的任务无法取消。

一次取消可能会停止调用方在同一 `contextId` 中的其他任务，因为一个回复回答了其中多条消息，
或者它们是一起排队的。它绝不会停止其他发送方的消息，例如对话所有者在控制台中输入的消息。
会停止这样的消息时，取消该任务的请求以 `TaskNotCancelableError` 被拒绝，错误中说明原因。
已经结束的任务也以同样的方式被拒绝。

`CancelTask` 在运行时停止后返回。如果对话的运行时仍在连接，它返回任务当前的状态，工作会在
运行时接收这条消息之前停止。

待处理的平台审批需要明确的交互 id 和回答；新的文本消息不算审批。

服务端接受文本任务（包括 `referenceTaskIds`），并提供结果和平台交互数据。它不接受上传的文件 part，也不接受推送
通知配置。`SendMessage`、`SendStreamingMessage`、`GetTask`、`ListTasks` 和
`SubscribeToTask` 读取的是同一批已记录的回复。新任务的消息在其 Session 启动期间就会
被接收；Session 的运行时就绪后，平台再把它交给 Agent。

流式客户端可以断开连接，已接受的执行不会因此取消。订阅先发送当前 Task，之后每当回复
已记录的状态或结果发生变化时，发出一次更新。
只有批准和拒绝两种选择的工具审批使用 `AUTH_REQUIRED`；表单，以及带有适配器声明选项的
决策，使用 `INPUT_REQUIRED`，Codex 的审批选项也属于这一类。
两者都会保持事件流打开。结构化回答会让原 Task 继续执行；该任务得到最终结果时，
事件流关闭。订阅已经结束的 Task，或向它发送新的回答，都会被拒绝。请用 `GetTask`
读取其结果。

`historyLength` 限制返回的 Task 历史长度。`acceptedOutputModes` 可以选择文本、JSON
或两者；只选一种格式时，其他结果 part 会转换成该格式，内容保持不变。不加限制地读取
Task 时，会保留回复的文本及其消息 part，与 Session 历史中显示的 part 相同。
运行时定位信息和原始诊断数据只保留在服务端。
协议定义以
[A2A 1.0 规范](https://a2a-protocol.org/v1.0.0/specification/)为准；
传输由 SDK 处理，AstraBox 则根据 Agent 运行时报告的回复确定任务的语义。
Hermes 原生的 A2A 对话文件和审计文件包含在 profile 状态归档中，位于用户工作区之外。

## 个人计划 {#personal-agent-schedules}

Assistant 内置的计划工具管理 `/api/v1/me/schedules`。它们复用平台的调度器、
Deployment 和运行记录（Run）。来源 Assistant 记录计划是在哪里创建的。带 `agent_id`
的计划由该 Agent 在自己的对话里执行；不带的由来源 Assistant 自己执行，每次运行是它
主对话里的一条消息（[例行任务与盯梢](assistants.md#routines-and-watches)）。

个人计划始终以经过认证的创建者身份执行。创建、启用或触发计划时都会检查对目标的访问
权限：创建者对 Agent 的使用权限，或对 Assistant 的归属。撤销访问权限后，新的工作会被阻止，而所有者仍可以查看、停用或删除该计划。其他用户
（包括目标 Agent 的所有者）无法通过该 Agent 的 Deployment 管理功能管理这个个人计划。
A2A 通信不会另建一套调度器。

打开账户设置菜单，选择**个人计划**即可查看这些计划。在对话框中选择一个计划，可以编辑
它的名称、提示词、cron 和时区，启用或停用它，立即运行，重放之前的一次运行，或删除它。
每条带有 Session 的运行记录都链接到对应的对话。要创建计划，告诉 Assistant 做什么、
什么时候做，以及是否交给某个 Agent。
