# Agent 间通信

Assistant 使用 Hermes 自带的 A2A 工具发现并调用其他 Agent。AstraBox 把 Assistant
所有者能够访问的 Agent 作为对端（peer）提供给它，所用凭证限定在该 Assistant 的工作区
和每个目标之内。目标在一个普通的 AstraBox Session 中执行，这个 Session 归调用方所有，
并使用目标配置的引擎。

Hermes 固定使用 0.22.4 版本，提交
`4d04303818b61bf3aa09278af48356315515965b`。它的
[A2A 插件](https://github.com/NousResearch/hermes-agent/blob/4d04303818b61bf3aa09278af48356315515965b/website/docs/user-guide/messaging/a2a.md)
提供 `a2a_discover`、`a2a_call`、`a2a_list`、`a2a_history` 和
`a2a_orchestrate`。AstraBox 执行由它管理的 Agent 时，不会启动 Hermes 的入站消息网关：
该网关有自己的另一套执行生命周期。

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

## 交互支持范围

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

限定于单个 Task 的取消会被拒绝：平台的中断会停止运行时正在进行的回复，而这个回复可能
同时回答多条排队消息，无法把取消限定在一个 A2A Task 上。待处理的平台审批需要明确的
交互 id 和回答；新的文本消息不算审批。

服务端接受文本任务，并提供结果和平台交互数据。它不接受上传的文件 part，也不接受推送
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
