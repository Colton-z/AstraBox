# Session

> 创建、运行、查看和归档 Agent Session。

Session 是与 Agent 进行的一段对话。它使用一个 Agent 和 Environment，保存你发送的
消息和 Agent 程序产生的回复，并报告 Agent 程序当前在做什么。你向 Session 发送消息，
回复会出现在 Session 的事件流中。

对话由 Agent 程序负责：程序是否正在工作、已经取走哪些消息、每条回复从哪里开始、
在哪里结束，都以程序的报告为准。AstraBox 记录并展示这些报告，并在程序取走你的消息
之前保存这些消息。

## Session 状态生命周期

Session 在 `state` 中报告以下状态之一：

| 状态 | 说明 | 可流转到 |
| --- | --- | --- |
| `CREATING` | 正在准备或分配运行环境。 | `READY`, `TERMINATED` |
| `READY` | 没有正在运行的工作，Session 可以接收消息。 | `PROCESSING`, `WAITING_INPUT`, `TERMINATED`, `DELETED` |
| `PROCESSING` | Agent 程序正在工作、有一条回复尚未结束，或 AstraBox 仍保存着程序尚未取走的消息。 | `READY`, `WAITING_INPUT`, `TERMINATED`, `DELETED` |
| `WAITING_INPUT` | 一条回复因审批或问题而暂停。 | `PROCESSING`, `READY`, `TERMINATED`, `DELETED` |
| `TERMINATED` | 运行环境已离线。 | `CREATING`, `DELETED` |
| `DELETED` | 已删除。 | —（终态） |

`runtime_status` 是 Agent 程序自己的报告：`working`、`waiting` 或 `idle`。
`PROCESSING` 和 `WAITING_INPUT` 由它以及 AstraBox 为这个 Session 记录的回复、问题
和消息推导得出。

还有两个生命周期标记与 `state` 一起公开：

- **已归档（archived）**：与 `state` 分开记录。Session 仍可读取，但不会出现在
  活动对话列表中。
- **运行环境不可用（runtime unavailable）**：Session 的沙箱已不存在或已释放时，
  `runtime_unavailable` 为 `true`。处于 `READY` 的 Session 保留对话历史，下一条
  消息会启动新的沙箱。

1. <b>创建 → CREATING</b> 新 Session 进入 `CREATING`，AstraBox 正在准备
   或分配运行环境。
2. <b>CREATING → READY</b> Session 已准备好，可以接收输入。Session 仍处于
   `CREATING` 时发送的消息会被保存，并在运行环境就绪后交给 Agent 程序。
3. <b>READY → PROCESSING</b> 发送消息后，消息会交给 Agent 程序，由程序开始
   一条回复。
4. <b>PROCESSING → WAITING_INPUT → PROCESSING</b> 需要审批或回答的回复会暂停，
   直到有人回应，然后继续。
5. <b>PROCESSING → READY</b> 回复结束，并且 Agent 程序报告自己已空闲。自行继续
   工作的程序（例如正在执行后台任务）会保持 `PROCESSING`，直到它报告空闲。
6. <b>TERMINATED</b> 对话仍然保留，但需要先恢复运行环境
   （`POST /api/v1/sessions/{session_id}/recover`），才能继续执行任务。
7. <b>DELETED（终态）</b> 删除 Session 后，它的生命周期结束。

恢复对话不依赖持久工作区卷。AstraBox 将 Agent 程序的原生会话数据保存到平台数据库，
更换计算资源时将这些数据恢复，交给程序自身的恢复操作使用。工作区文件的生命周期是
独立的：要在沙箱丢失或释放后保留这些文件，需要配置可选的持久工作区存储。连接断开本身不代表
沙箱已经失效；AstraBox 会重新连接程序，并从已记录的位置继续。确认沙箱丢失后，
进行中的回复以失败结束。程序尚未取走的消息会自动交给新的沙箱，由它恢复对话；程序
已经取走的消息不会再次发送。

## 模型请求自动重试

所选 Agent 程序或模型网关执行的重试属于进行中的回复。Session 保持 `PROCESSING`，
AstraBox 不会另外公开 `rescheduling` 状态。请继续接收事件流，不要在回复尚未结束时
重复发送同一条消息。

事件流报告错误时，应以回复已记录的 `outcome` 和之后的 Session 状态为准。重试分类由
所选 Agent 程序或模型网关负责；AstraBox 不会另行构造一套重试状态。

## 中断语义

```bash
# 中断进行中的回复
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/interrupt" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

- **`status: "accepted"`**：AstraBox 把请求交给 Agent 程序，由程序停止进行中的
  回复，并报告回复的结束方式，例如 `cancelled`。
- **`status: "idle"`**：未观察到进行中的回复或平台保留的输入，不表示运行环境已断开。
- **`409 PENDING_INPUTS_NOT_CANCELLED`**：未观察到进行中的回复，但平台仍保留已接收输入。
  `data.input_ids` 列出读取时的这些输入；它们未被撤回，仍可能执行。控制台会解释这次拒绝，
  不会把它显示为成功空闲。
- **中断后**：Session 仍可复用，照常发送下一条消息即可。

只有 `DELETED` 是终态。中断回复不会删除 Session 或已保存的对话历史。Session 的
网页终端有自己的停止操作 `POST /api/v1/sessions/{session_id}/terminal/interrupt`；
中断对话不会停止终端命令。

中断不会撤回排队消息，即使它停止了当前回复。已接收的单条输入没有撤回操作。
`pending` 记录只表示交付回执尚未到达，运行环境可能已经收到输入；不要通过删除记录或
把它标为已取消来实现撤回。

需要让整个对话离线时，在对话列表中选择**归档**
（`POST /api/v1/sessions/{session_id}/archive`）。归档成功后不再建立运行环境连接，
保存的历史仍可读取；读取方核对归档状态时，尚未交给程序的输入以 `CONVERSATION_ENDED`
失败。归档是独立的生命周期操作，不是取消单条消息，也不会撤销程序已经执行的工作。
归档发生错误时，应先处理错误，再将对话视为离线。

## Agent 工作时发送消息

除 `TERMINATED` 和 `DELETED` 外，Session 在任何状态下都接收消息。AstraBox 按接收
顺序把消息交给 Agent 程序。正在工作的程序会把消息放进自己的队列，准备好后再回答；
逐条回答还是一起回答排队的消息，由程序决定。每条回复都会在 `input_ids` 中列出它
回答的消息。

向 `TERMINATED` 或已删除的 Session 发送消息会返回 `HTTP 409 AGENT_RUNTIME_ERROR`。
请先恢复 `TERMINATED` 状态的 Session。待处理的审批或问题需要通过交互回答接口回答，
不能通过发送消息回答。

## 创建 Session

Session 从 Agent 启动，Environment 已经由这个 Agent 选定。在控制台中：

1. 在 AstraBox 控制台中打开 **Agent**。
2. 选择一个 Agent。
3. 选择 **开始对话**。

运行环境准备好后，新的 Session 会自动打开。Agent 开启预热时，Session 可以领取在它
创建之前就已准备好的运行环境，其中已包含 Agent 配置和已配置的扩展。配置持久工作区
存储后，AstraBox 会在交付运行环境前选定新 Session 的工作区，或已有 Session 保存的
工作区。运行环境使用期间，这个分配关系保持固定。

外部系统可以在请求路径中传入 Agent ID：

```bash
# 使用 Agent ID 创建 Session
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/agents/$AGENT_ID/conversations" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: create-code-review-session' \
  --data '{}'
```

请求成功后返回 Session ID：

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "session_id": "SESSION_ID",
    "agent_id": "AGENT_ID",
    "deployment_name": "code-reviewer"
  }
}
```

`Idempotency-Key` 请求头可选。同一用户和 Agent 重复使用同一个 key 时，
AstraBox 会返回同一个 Session，不会重复创建。

创建响应会标识 Session、Agent 和 Deployment。之后通过 `GET
/api/v1/sessions/{session_id}` 读取 Session 对象，常用字段如下：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `session_id` | string | 系统生成的 Session ID。 |
| `agent_id` | string | 启动这个 Session 的 Agent。 |
| `state` | string | Session 当前状态。 |
| `title` | string/null | Session 标题。 |
| `model_name` | string/null | 当前运行环境报告的模型。 |
| `permission_mode` | string/null | Agent 程序支持时，为它选择的权限模式。 |
| `runtime_status` | string/null | Agent 程序最近一次报告的状态：`idle`、`working` 或 `waiting`。 |
| `current_responses` | array | Agent 程序正在产生的回复。 |
| `last_response` | object/null | 最近一条已结束的回复，包含 `outcome`（`completed`、`failed` 或 `cancelled`）和程序自己给出的 `native_reason`。 |
| `pending_interaction` | object/null | Session 正在等待的审批或问题。 |
| `held_inputs` | array | 已接收、但 Agent 程序尚未取走的消息。 |
| `output_seq` | integer | 跟随 Session 事件流时使用的起始输出位置。 |
| `runtime_unavailable` | boolean | Session 的沙箱是否已不存在或已释放。 |
| `created_at` | string/null | 创建时间。 |
| `updated_at` | string/null | 最后更新时间。 |

Session 列表响应包含每个 Session 的摘要，其中的 `state` 来自 Agent 程序最近一次
报告的状态；`GET /api/v1/sessions/{session_id}` 返回当前完整详情。Agent 程序为某条
回复报告 token 用量或模型费用后，控制台会把这些数据和该回复一起显示。计费和限额由
当前部署配置的模型服务管理。

AstraBox 创建 Session 时不接受 Agent 对象、Agent 版本或 Environment 请求字段。
请求路径指定 Agent，而 Environment 已经由 Agent 选定。Session 记录 Agent 身份，
不会快照或锁定某个 Agent 版本：

- AstraBox 第一次为 Session 准备运行环境时，解析当前 Agent 和 Environment
- 保存 Agent 或 Environment 后，正在运行的运行环境不会被原地重新配置
- AstraBox 后续重建运行环境时，同一个 Session 可以使用当前保存的 Agent 和
  Environment 配置
- Agent 的 `version` 字段用于检测并发更新，不是 Session 可以选择的配置版本

## 创建后添加资源

AstraBox 创建 Session 的请求不接受 `resources` 数组。默认 GitHub 仓库和 Vault
分配关系配置在 Agent 上；一次性文件则在 Session 运行环境准备好后直接上传到工作区。
参阅[访问 GitHub](working-with-repos.md)、[使用 Vault 认证](credentials.md)和
[上传与下载文件](files.md)。

## 发送消息 {#send-a-message}

管理员配置转写模型后，可以点击控制台的麦克风按钮录音，将转写文字追加到可编辑草稿，
检查后再发送。配置方式、录音限制和重试行为见[语音输入](voice-input.md)。

`POST /api/v1/sessions/{session_id}/inputs` 接收一条消息，并立即返回回执。对这条
消息的回复会出现在 Session 的事件流中。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `content` | string | 是 | 用户消息。 |
| `client_message_id` | string | 否 | 调用方为这条消息生成的幂等 key。 |

如需更改 Agent 程序使用的权限模式，请更新 Session 的权限模式（参见
[读取和更新 Sessions](#read-and-update-sessions)）。

```bash
# 发送一条消息
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/inputs" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{
    "content": "分析当前目录下所有 Python 文件的代码复杂度。",
    "client_message_id": "analysis-1"
  }'
```

回执会标明已接收的消息：

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "session_id": "SESSION_ID",
    "input_id": "INPUT_ID",
    "client_message_id": "analysis-1",
    "accepted_at": "2026-05-18T12:00:00Z",
    "state": "pending",
    "duplicate": false
  }
}
```

再次发送相同的 `client_message_id` 会返回第一次接收的消息，并带有
`duplicate: true`；Agent 程序只会收到一次。部署的准入策略可以用
`HTTP 429 ADMISSION_DENIED` 拒绝消息。

被 Agent 程序或部署的运行准备流程拒绝的消息不会开始回复：事件流会用带有原因的
`data-input-failed` part 报告它，这条消息也会在 Session 历史中保留它的 `failure`。

## 读取 Session 事件流

事件流使用 AI SDK UI Message Stream 协议，从你在 `after_seq` 中传入的位置之后开始，
把每条回复作为一条 UI 消息发送。要等待下一条回复，请从 Session 的 `output_seq`
开始；`GET /api/v1/sessions/{session_id}` 和消息列表的第一页都会返回这个值。新
Session 的输出从 `0` 开始：

```text
GET /api/v1/sessions/{session_id}/stream?after_seq=0
```

`data-resume-cursor` part 的 `data.seq` 带有最新位置。保存该值，并通过 `after_seq`
重新连接；AstraBox 不使用 SSE `Last-Event-ID` 请求头：

```text
GET /api/v1/sessions/{session_id}/stream?after_seq=42
```

一次事件流请求会在结束一条回复的 part 之后结束。使用保存的 `after_seq` 打开下一次
请求，接收后续回复。持久保存的对话历史通过 `GET /api/v1/sessions/{session_id}/messages`
读取，并使用 `before` 向更早的消息分页。Part 格式和断线重连方式见
[SSE 事件流](events-stream.md)。

## 回答审批和问题

回复因审批或问题暂停时，事件流会发送 `data-interaction` part（工具审批还会发送
`tool-approval-request`），Session 也会在 `pending_interaction` 中报告它。使用
交互 ID 回答：

```bash
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/interactions/$INTERACTION_ID/answer" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"answer": {"decision": "approve"}}'
```

回答的格式取决于交互的 `presentation`，参见[权限模式](permission-modes.md)。已被
回答、撤回或已过期的交互会返回 `HTTP 409 INTERACTION_NOT_OPEN`。回复会在 Session
事件流中继续。

## 读取和更新 Sessions {#read-and-update-sessions}

```bash
# 获取一个 Session
curl --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID" \
  --header "Authorization: Bearer $ACCESS_TOKEN"

# 分页列出 Sessions
curl --silent --show-error \
  "$SERVICE_URL/api/v1/sessions?page=1&limit=10" \
  --header "Authorization: Bearer $ACCESS_TOKEN"

# 更新这个 Session 的 Agent 程序权限模式
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/permission-mode" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data "{\"permission_mode\":\"$PERMISSION_MODE\"}"
```

分页响应示例：

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "sessions": [
      {
        "session_id": "SESSION_ID",
        "agent_id": "AGENT_ID",
        "state": "READY",
        "runtime_status": "idle",
        "title": "code-reviewer",
        "created_at": "2026-05-18T12:00:00Z",
        "updated_at": "2026-05-18T12:30:00Z"
      }
    ],
    "has_more": false,
    "next_cursor": null
  }
}
```

分页响应使用 `has_more` 和 `next_cursor`。未传 `page=1` 时，同一个列表接口返回
控制台使用的非分页数组。这个 Session 路由族只能更新权限模式这一项 Session 配置；
具体可选值由所选 Agent 程序定义。AstraBox 把权限模式保存在 Session 上，并应用到
正在运行的 Agent 程序；没有运行中程序的 Session 会在运行环境启动时获得该模式。

### 对话历史与工作折叠

`GET /api/v1/sessions/{session_id}/messages` 以 UI 消息返回保存的对话。控制台通过
`GET /api/v1/sessions/{session_id}/history-blocks` 读取相同消息，将折叠的工作替换为
`data-process-block` 标题。标题的 `block_id` 和 `cursor` 对应
`GET /api/v1/sessions/{session_id}/history-blocks/{block_id}?cursor=...` 中的内容；
展开时按记录的位置获取。

以 `failed` 或 `cancelled` 结束的回复可以把已完成的工作和中间正文折叠到一个标题下。
成功回复在最后一次工具调用之后有正文时，也可以折叠；该调用之后的正文留在折叠区域外。
`max_tokens` 等原生结束原因不会选择另一套布局。回复元数据独立保留 `outcome` 和
`native_reason`。

控制台的显示规则如下：

- 失败提示和停止提示始终留在折叠区域外。
- 异常结束时，失败、被拒绝和未完成的工具调用不会折进整条回复。交互与结果 part 也留在
  外部；存在待审批工具时，不能折叠整条回复。
- 展开整条回复会恢复正文和过程分组；展开普通过程分组会直接显示工具与推理。
  工具卡片保留自身的输入、输出展开控制。折叠只改变初始显示，不会丢弃步骤。
- 重新加载通过历史接口读取相同的回复结果与内容。生成了过程标题，不会把失败回复变为成功。

过程摘要用于命名操作，不代表回复结果。在过程摘要接口上，`status: completed` 表示标题
生成完成。失败或取消的回复即使有标题，`response_completed` 仍为 `false`。
集成方应依据记录的 `outcome` 判断成功与否；只要失败提示和全部步骤仍可访问，中间正文可以折叠。

### 未读回复

Session 会记录最后一次产生输出的时间（`last_output_at`），无论输出由某人的消息引起，
还是来自 Agent 程序自己发出的回复，例如 Assistant 的定时任务或心跳。属主的 `read_at`
记录其最后一次阅读的时间：

```bash
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/read" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

`last_output_at` 晚于 `read_at` 的 Session 为未读。控制台在可见标签页中打开对话时
将其标记为已读，并在列表里未读对话旁显示圆点。开启通知后（列表中 **会话** 旁的
铃铛），只要有控制台标签页打开（包括后台标签页），每条新的未读回复都会弹出浏览器通知。
回复结束时（包括 Agent 程序自行开始的回复），它所在的对话会移到列表顶部。

## Child runs

Agent 程序可以把工作委派给 child run。AstraBox 通过 Session 级接口公开它们的树形
关系、引擎原生状态、消息、用量摘要和当前支持的操作：

```text
GET /api/v1/sessions/{session_id}/child-runs
GET /api/v1/sessions/{session_id}/child-runs/{child_run_id}/messages
POST /api/v1/sessions/{session_id}/child-runs/{child_run_id}/stop
```

**Agents** 标签展示同样的 child run 信息。委派方式、共享工作区和 child run 控制操作见
[Multiagent 编排](multi-agents.md)。

## 生命周期

需要让 Session 离开活动对话列表、但仍可读取时，将其归档：

```text
POST /api/v1/sessions/{session_id}/archive
```

需要让记录和运行环境进入终态 `DELETED` 时，删除 Session：

```text
DELETE /api/v1/sessions/{session_id}
```

结束或删除 Session 与中断当前回复不同。应根据需要保留的数据选择对应的生命周期操作。

Agent 程序报告自己正在工作时，归档、删除、结束对话和终止其沙箱都会返回
`HTTP 409 SESSION_BUSY`。请先中断工作，或等待它完成。正在等待审批或问题回答的
Session 不算在工作，可以归档或删除。

## Browser

Browser 面板通过有签名、会过期的查看链接控制沙箱桌面。单次 Agent 对话独占的沙箱，以及
当前登录用户拥有的 Assistant 工作区可以使用它。Assistant 的多个 Session 共用其归属用户
的桌面。多个用户共用的 Agent 沙箱没有按对话隔离桌面，因此不能开放 Browser。

工作区休眠时，先唤醒 Assistant 再打开 Browser。重新连接会为工作区当前沙箱生成新链接，
替换沙箱后也一样。桌面标签页和 Browser 链接属于该沙箱，不会随替换保留。
这个查看器不要求持久工作区卷。

## 对话工作流

Session 支持包含多条消息的对话。推荐流程如下：

1. 通过 `POST /sessions/{id}/inputs` 发送用户消息。
2. 从最后保存的游标（或 Session 的 `output_seq`）开始跟随
   `GET /sessions/{id}/stream`，读取回复直到它的 `finish` part。
3. 回答回复暂停时提出的审批或问题。
4. 向同一个 Session 发送下一条消息。Agent 程序仍在工作时发送的消息会在程序的队列
   中等待。

```bash
BASE_URL="$SERVICE_URL/api/v1"

# 第一条消息：提出需求
curl --silent --show-error --request POST \
  "$BASE_URL/sessions/$SESSION_ID/inputs" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"content":"创建一个 Python Flask 项目脚手架。","client_message_id":"message-1"}'

# 读取回复；新 Session 的输出从 0 开始
curl --no-buffer --silent --show-error \
  "$BASE_URL/sessions/$SESSION_ID/stream?after_seq=0" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Accept: text/event-stream'

# 第二条消息：追加要求
curl --silent --show-error --request POST \
  "$BASE_URL/sessions/$SESSION_ID/inputs" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"content":"给项目添加单元测试和 CI 配置。","client_message_id":"message-2"}'
```

## 分享 Session

打开 Session 并选择**分享**，即可生成只读链接。可以设置有效期，并决定查看者是否能够
下载工作区文件。链接本身授予读取权限，因此只应发送给目标查看者，并在不需要时撤销。
查看者不能发送消息或修改 Session。

## 导入早期历史 {#import-earlier-history}

把对话从其他系统迁移到 AstraBox 时，平台管理员可以导入每段对话的早期消息，让对应
Session 显示这些消息。导入只用于阅读：它不会启动沙箱、不会执行任何操作，也不会改变
Session 状态。Agent 程序不会读取这些消息；要从中断处继续对话，请另外恢复程序自身的
会话历史。

```bash
curl -X POST "$ASTRABOX/api/v1/admin/sessions/$SESSION_ID/history-import" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"messages": [
    {"source_id": "m-1", "role": "user", "created_at": "2026-01-05T09:00:00Z",
     "blocks": [{"type": "text", "text": "总结这次事故。"}]},
    {"source_id": "m-2", "role": "assistant", "created_at": "2026-01-05T09:00:20Z",
     "completion": "unknown", "parent_source_id": "m-1",
     "blocks": [{"type": "text", "text": "这次故障开始于"}]}
  ]}'
```

- 按对话顺序列出消息，每次调用最多 500 条。`source_id` 是消息在原系统中的标识；
  `created_at` 需要带时区。
- 用户消息包含 `text` 和 `image` 块。回复还可以包含 `thinking`、`tool_use`，以及
  回答同一条回复中较早工具调用的 `tool_result`。
- 回复必须包含 `completion`。原系统从未记录回复如何结束时，使用 `unknown`；控制台
  会把它显示为已导入但未记录结束方式，而不是成功或失败。
- `parent_source_id` 记录一条消息回复的是哪条消息。
- 请在 Session 收到它自己的第一条消息之前导入。对话开始后，导入会被拒绝并返回
  `HISTORY_IMPORT_AFTER_CONVERSATION`，因为历史不能放到已有消息之前。
- 重复执行同一次导入不会产生任何变化。要继续一次导入，先发送已经导入的消息，再发送
  新的消息。已使用的 `source_id` 下出现其他内容，或新消息被放在已导入消息之前时，
  请求会被拒绝并返回 `HISTORY_IMPORT_CONFLICT`。

导入的消息和其他消息一样可以读取、分页和分享，并带有 `history_import`，其中包含
`source_id`、`completion` 和 `parent_source_id`。

## 最佳实践

1. **把 Agent 配置视为实时配置** — Session 不锁定 Agent 版本。保存 Agent 不会
   修改正在运行的运行环境，但重建后的运行环境会使用当前保存的 Agent 和 Environment
   配置。
2. **使用稳定的请求标识** — 为每个 Session 创建请求设置稳定的
   `Idempotency-Key`，并为每条消息设置稳定的 `client_message_id`，以避免重复请求
   并便于追踪。
3. **明确区分中断和归档** — 需要停止某条回复时使用中断；只有在 Session 应离开
   活动列表并释放运行环境时才归档。

## 常见问题

**Q：在 Agent 工作时发送消息会怎样？**

A：AstraBox 会接收这条消息并交给 Agent 程序，程序把它放进队列，在当前工作完成后
回答，也可能与其他排队消息在同一条回复中一起回答。回复的 `input_ids` 会列出它回答
的消息。

**Q：消息被拒绝后，能用同一个 `client_message_id` 再发一次吗？**

A：这样得不到不同的结果。同一个 `client_message_id` 始终指向同一条消息，因此再次
发送会返回第一次的回执以及这条消息已记录的 `failure`。请使用新的
`client_message_id`，把这段文字作为新消息再发一次。

**Q：中断后还能继续使用 Session 吗？**

A：可以。发送下一条消息即可继续。中断回复不会删除 Session 或它的历史记录。

**Q：如何获取 Session 的完整对话历史？**

A：调用 `GET /sessions/{id}/messages`，并使用每页返回的 `before` 游标向更早的消息分页。
消息是 AI SDK UI 消息：你发送的每条消息对应一条，每条回复也对应一条。回复进行中时，
第一页会包含到目前为止生成的 part，以及 `output_seq`，即继续跟随 Session 事件流读取
剩余内容的位置。

**Q：SSE 断线后如何重连？**

A：保存最新 `data-resume-cursor` part 的 `data.seq`，然后使用 `after_seq`
查询参数重连。AstraBox 不读取 `Last-Event-ID`。

**Q：创建或编辑 Agent 时为什么找不到某个 Environment？**

A：Agent 表单只列出已启用、并且其 Agent 程序支持 Agent 的 Environment。
**运行环境**管理页也会显示已停用项；请在那里检查 Environment 的启用状态和
所选 Agent 程序。

## API 参考

- 当前部署实例的 `/docs` 与 `/openapi.json` 参考
- [SSE 事件流](events-stream.md) — 接收输出并安全地断线重连
- [Multiagent 编排](multi-agents.md) — 查看和控制 child run
- [上传与下载文件](files.md) — 使用 Session 工作区
