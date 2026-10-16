# SSE 事件流

AstraBox 使用 [AI SDK UI Message Stream
协议](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol)，通过 **Server-Sent Events (SSE)**
流式发送 Session 的公开输出。Agent 程序每产生一个 part，连接就会把它发送出来，
无需轮询。

## 连接 URL

```text
GET /api/v1/sessions/{session_id}/stream?after_seq=42
```

请求头：

```text
Authorization: Bearer $ACCESS_TOKEN
Accept: text/event-stream
```

事件流先发送 Session 在 `after_seq` 之后已记录的 part，然后等待并实时发送新的
part。每个 part 只发送一次。一次连接会在结束一条回复的 part（`finish` 或 `error`）
之后结束，因此每次连接承载一条回复；使用最后收到的游标打开下一次连接，等待
后续回复。关闭连接不会停止回复或 Agent 程序。

`after_seq` 默认为 `0`，即 Session 输出的开头。要等待下一条回复，而不是读取 Session
的第一条回复，请从 `GET /api/v1/sessions/{session_id}` 和
`GET /api/v1/sessions/{session_id}/messages` 第一页返回的 `output_seq` 开始。

消息通过 `POST /api/v1/sessions/{session_id}/inputs` 单独发送，参见
[Session](sessions.md)。响应通过 `x-vercel-ai-ui-message-stream: v1` 声明协议版本。
AstraBox 会记录它发送的每个 part，因此网络中断后客户端可以重新连接，不会丢失输出。
AstraBox 不使用 SSE `Last-Event-ID` 请求头；请按下文说明使用 `after_seq` 继续接收。

## SSE 格式

每个 part 都是 SSE `data` 字段中的 JSON 对象：

```text
data: {"type":"start","messageId":"RESPONSE_ID","messageMetadata":{"response_id":"RESPONSE_ID","input_ids":["INPUT_ID"],"origin":"input"}}

data: {"type":"text-start","id":"text-1"}

data: {"type":"text-delta","id":"text-1","delta":"你好"}

data: {"type":"text-end","id":"text-1"}

data: {"type":"finish","finishReason":"stop"}

data: {"type":"data-resume-cursor","transient":true,"data":{"seq":42}}

data: [DONE]
```

| 字段 | 说明 |
| --- | --- |
| `type` | AI SDK 消息 part 类型，决定 JSON 其余部分的结构。 |
| `id` 或 `toolCallId` | 相关的 start、delta 和 end part 共用的标识。 |
| `data` | AstraBox `data-*` part 的载荷。 |

连续 15 秒没有输出时，服务端会发送 keep-alive 注释。

## 回复

回复是 Agent 程序给出的一次答复，从开始一直到结束，开始和结束都以程序的报告为准。
回复的 part 以 `start` part 开头，它的 `messageId` 是回复 ID，`messageMetadata`
包含：

| 字段 | 含义 |
| --- | --- |
| `response_id` | 回复的 ID。 |
| `input_ids` | 这条回复回答的消息。一条回复可以回答多条排队的消息。 |
| `origin` | 回复回答消息时为 `input`；Agent 程序自行开始回复时为 `engine`，例如后台任务完成或定时任务运行之后。`engine` 回复之前没有用户消息。 |

回复以 `finish` part 结束。失败的回复会在 `finish` 之前发送 `error` part。

## 消息增量

文本增量输出以 `text-start` 开始，随后输出一个或多个 `text-delta` part，最后
输出 `text-end`：

```text
data: {"type":"text-start","id":"text-1"}

data: {"type":"text-delta","id":"text-1","delta":"你好"}

data: {"type":"text-end","id":"text-1"}
```

所选 Agent 程序提供推理内容时，使用同样的结构：

```text
data: {"type":"reasoning-start","id":"reasoning-1"}

data: {"type":"reasoning-delta","id":"reasoning-1","delta":"正在检查代码仓库"}

data: {"type":"reasoning-end","id":"reasoning-1"}
```

同一段文本或推理内容的所有 part 使用相同 `id`。工具输入使用对应的
`tool-input-start`、`tool-input-delta` 和 `tool-input-available` 序列，并通过
`toolCallId` 关联。

## 消息增量期间重连

断线重连时，将 `data-resume-cursor` part 中最近一次收到的 `seq` 作为 `after_seq`。
事件流从该位置之后的第一个 part 继续，因此保留了当前回复解析器状态的客户端可以从
中断处准确继续，不会重复或遗漏 part。

丢失了解析器状态的客户端（例如重新加载的页面）应改为通过
`GET /api/v1/sessions/{session_id}/messages` 读取对话。第一页会把进行中回复已记录的
所有 part 合并到该回复的消息中，并返回 `output_seq`；从 `output_seq` 开始跟随事件流，
就能把这条回复的剩余部分恰好补充一次。

## 事件类型

事件流遵循 AI SDK 协议，不另外定义 AstraBox 自己的事件词汇。集成通常需要处理以下
几类 part：

| Part 类别 | 含义 | 示例 |
| --- | --- | --- |
| 消息和步骤生命周期 | 回复开始或结束，或者一次模型步骤开始或结束。 | `start`, `start-step`, `finish-step`, `finish` |
| 文本和推理 | Agent 的增量输出。 | `text-*`, `reasoning-*` |
| 工具调用 | 工具输入、审批和结果。 | `tool-input-*`, `tool-approval-request`, `tool-output-*` |
| 交互 | 需要回应的问题、决定或审批。 | `data-interaction` |
| 失败的消息 | 不会得到回答的消息；它不会开始回复。 | `data-input-failed` |
| 工具卡片 | 平台工具为对话记录的卡片（例如 Artifact），绑定到对应的工具调用。 | `data-tool-card` |
| Session 变更 | 临时通知：Session 记录发生变化，例如生成了标题；请重新读取 Session。 | `data-session-changed` |
| Child run | child run 的生命周期或消息；当前状态请读取 Session 的 child-run 接口。 | `data-subagent` |
| 文件变更 | 工具调用修改的文件。 | `data-file-changes` |
| 结果 | Agent 程序为回复报告的用量或费用，前提是其适配器会报告这些数据。 | `data-result` |
| 恢复 | 提供重新连接使用的游标。 | `data-resume-cursor` |
| 错误 | 报告失败的回复或事件流故障。 | `error` |

标准 AI SDK part 保持其文档中定义的结构。`data-*` part 在 `data` 中携带一个对象；
你的集成没有记录的字段应视为新增字段。

## 常见事件流

```text
start
start-step
reasoning-start / reasoning-delta / reasoning-end  （可选）
text-start / text-delta / text-end
tool-input-* / tool-output-*                       （可选）
finish-step
data-result                                        （可选）
finish
data-resume-cursor
[DONE]
```

并非每条回复都包含全部 part。Agent 程序可以产生多次模型步骤和工具调用。不属于任何
回复的 part（例如 child run 的状态）可能在两条回复之间到达，它们不会结束连接。

### 模型结果

`data-result` 包含 Agent 程序为这条回复报告的用量或费用数据。可用字段因 Agent 程序
和模型服务而异；例如，Claude Code 适配器会报告这些数据：

```json
{
  "type": "data-result",
  "data": {
    "total_cost_usd": 0.0137,
    "usage": {
      "input_tokens": 1204,
      "output_tokens": 88
    }
  }
}
```

解析时应允许 `data` 对象增加字段，也不要假设所有 Agent 程序都会报告每项
数据。

### 失败的消息

不会得到回答的消息会在事件流中报告，而不会产生回复。原因可能是：Agent 程序或部署的
运行准备流程拒绝了它，对话在它被取走之前已经结束，或者需要用户先完成操作，它的运行
环境才能启动。

```json
{
  "type": "data-input-failed",
  "data": {
    "input_id": "INPUT_ID",
    "code": "TURN_PREPARATION_FAILED",
    "message": "sandbox turn preparation failed"
  }
}
```

`data` 还可以包含 `status_code`；需要用户先采取操作时
（`TURN_PREPARATION_USER_ACTION_REQUIRED`），还会包含 `user_action`，其中有一条说明
和可选的 URL。失败的消息不会被再次发送；请把它的文字作为新消息发送。

## 连接生命周期

- `finish` part 结束当前回复，连接则在随后的 `data-resume-cursor` part 和 `[DONE]`
  之后结束。
- `error` part 报告失败的回复并结束连接。它的 `data-resume-cursor` part 会在
  `error` part 之前到达，因为 AI SDK 读取器遇到错误就会停止。在 Session 的输出中，
  这条回复自己的 `finish` part 位于 `error` part 之后，因此下一次连接会从它开始。
  随沙箱一起丢失的回复也以这种方式结束，其 `finish` part 带有
  `messageMetadata.outcome: "failed"`。决定是否重新发送消息之前，请先查看 Session。
- `data-interaction` part 之后出现的 `finishReason: "tool-calls"` 的 `finish` part
  标记一个交互点：回复只是暂停，并未结束。交互得到回答后，回复会在下一次连接中以
  同一个 `messageId` 继续，并以新的 `start` part 开头。
- 网络中断时使用 `after_seq` 重连。Session 已删除或当前用户无权访问时，下一次连接
  会返回 HTTP 错误。

## 工具调用配对

每次完成的工具调用都通过 `toolCallId` 关联。

关键的工具输入 part：

```json
{
  "type": "tool-input-available",
  "toolCallId": "tool-call-1",
  "toolName": "Bash",
  "input": {"command": "pwd"}
}
```

对应的输出使用相同的 `toolCallId`：

```json
{
  "type": "tool-output-available",
  "toolCallId": "tool-call-1",
  "output": "/workspace"
}
```

失败或被拒绝的调用使用 `tool-output-error` 或 `tool-output-denied`。请按事件流顺序
处理 part，不要按标识对工具调用排序。

## 回应工具请求

事件流产生 `data-interaction` part 或 `tool-approval-request` 时，使用 part 中的
交互 ID，向 `POST
/api/v1/sessions/{session_id}/interactions/{interaction_id}/answer` 发送回答：

```json
{
  "answer": {
    "decision": "approve"
  }
}
```

回答格式由交互请求的 `presentation` 决定：表单包含问题答案，决策选择请求中
声明的 option ID，工具审批使用 `approve` 或 `reject`。完整示例见
[权限模式](permission-modes.md)。

## `finish` 完整结构

终止的 `finish` part 给出 AI SDK 的结束原因：

```json
{
  "type": "finish",
  "finishReason": "stop"
}
```

AstraBox 随后发送一个临时游标：

```json
{
  "type": "data-resume-cursor",
  "transient": true,
  "data": {
    "seq": 42
  }
}
```

传输层随后发送 `data: [DONE]` 并关闭响应。请保存 `seq`；不要从消息、内容块或工具
标识推导游标。

## 使用 `after_seq` 重连

新连接默认从头发送 Session 的输出。要从已收到的游标继续，请传入 `after_seq`：

```text
GET /api/v1/sessions/{session_id}/stream?after_seq=42
```

AstraBox 在每批 part 之后发送一个 `data-resume-cursor` part。每个游标指向最后发送的
part，因此使用它重连会从下一个 part 继续。

> 对于长时间运行的连接，每当收到 `data-resume-cursor` part 时都更新保存的游标，
> 并在下一次请求中使用它的 `seq`。

## Frame 发送节奏

AstraBox 不公开 `delta_flush_interval_ms`。文本、推理和工具输入 part 会在所选
Agent 程序产生时立即转发。没有输出时，传输层可能发送 keepalive 注释。

## 事件历史

持久保存的对话历史和分页请使用 messages 接口：

```bash
curl --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/messages?limit=20" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

响应包含 AI SDK UI 消息：发送给 Session 的每条消息对应一条，每条回复也对应一条，
回复由事件流发送的同一批 part 合并而成。使用 `before` 向更早的消息分页。要继续接收流式
输出，请使用上文的 `data-resume-cursor` 和 `after_seq` 流程。

## curl 示例

```bash
curl --no-buffer --silent --show-error \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Accept: text/event-stream' \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/stream"
```

使用 `after_seq` 继续接收：

```bash
curl --no-buffer --silent --show-error \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Accept: text/event-stream' \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/stream?after_seq=42"
```

## JavaScript EventSource 示例

浏览器原生的 `EventSource` 使用已登录的同源 cookie，无法添加 `Authorization`
请求头；使用 bearer token 的客户端应改用 `fetch` 和 SSE 解析器。

```javascript
let afterSeq = 0;

function followSession(sessionId) {
  const url = new URL(`/api/v1/sessions/${sessionId}/stream`, window.location.origin);
  url.searchParams.set('after_seq', String(afterSeq));

  const stream = new EventSource(url, {withCredentials: true});

  stream.onmessage = (event) => {
    if (event.data === '[DONE]') {
      stream.close();
      followSession(sessionId);
      return;
    }

    const part = JSON.parse(event.data);
    if (part.type === 'text-delta') {
      console.log('Agent:', part.delta);
    }
    if (part.type === 'data-resume-cursor') {
      afterSeq = part.data.seq;
    }
    if (part.type === 'error') {
      console.error('Error:', part.errorText);
    }
  };

  stream.onerror = () => {
    stream.close();
    followSession(sessionId);
  };
}
```

## 客户端实现建议

1. <b>记录最后的游标</b> — 保存 `data-resume-cursor.data.seq`，断线后将其作为
   `after_seq` 使用。
2. <b>使用 AI SDK 解析器</b> — 保持文本、推理和工具 part 的 start/delta/end 顺序
   和 ID。
3. <b>关注回复结束</b> — 回复正常结束后，打开新的连接等待下一条回复。出错时查看
   Session。
4. <b>传输中断时重连</b> — 从最后的游标重新打开连接；不要根据最后渲染的文本 delta
   推测位置。
5. <b>处理文档中列出的 data part</b> — 处理客户端需要的 `data-*` 类型，忽略它无法
   理解的新增字段。

## 常见问题

<b>Q：连接时收到大量历史事件，如何避免？</b>

A：传入 `after_seq`，值为最近一个 `data-resume-cursor` part 中的 `seq`，或 Session
的 `output_seq`。位于该位置及之前的 part 不会发送。

<b>Q：SSE 连接会自动关闭吗？</b>

A：一条回复结束后，连接会关闭。使用 `after_seq` 打开下一次连接，等待后续回复。
Session 空闲时，当前连接保持打开并接收 keepalive。

<b>Q：可以改用 WebSocket 吗？</b>

A：Session 消息流使用 SSE。SSE 适合单向输出流，并支持通过 AstraBox 记录的游标重新
连接。

<b>Q：可以设置 `delta_flush_interval_ms` 吗？</b>

A：不可以。AstraBox 按所选 Agent 程序产生 part 的节奏转发，不提供单独的刷新间隔。
