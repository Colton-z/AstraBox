# SSE 事件流

AstraBox 通过 **Server-Sent Events (SSE)** 和 [AI SDK UI Message Stream
协议](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol)流式输出 Session 公开内容。

## 连接 URL

```text
GET /api/v1/sessions/{session_id}/ai-stream?follow=session
```

请求头：

```text
Authorization: Bearer $ACCESS_TOKEN
Accept: text/event-stream
```

`follow=session` 可以在 Session 空闲时建立连接。连接会等待输出，返回一条
完整的助手回复，然后关闭。使用最近一次收到的 `after_seq` 游标重新连接，
继续等待下一条回复。一轮平台任务可以依次处理多条排队输入并产生多条回复；
某条回复的连接关闭，不会停止该轮任务或其引擎。

也可以在同一个请求中发送消息并接收本轮结果：

```text
POST /api/v1/sessions/{session_id}/ai-stream
```

响应通过 `x-vercel-ai-ui-message-stream: v1` 声明协议版本。可重放的语义 frame
会在流式输出时持久化，因此网络中断后可以继续接收；实时 delta 在持久记录中
可能会被合并。AstraBox 不使用 SSE `Last-Event-ID` header；断线重连使用下文
说明的 `after_seq`。

## SSE 格式

每个 frame 都是 SSE `data` 字段中的 JSON 对象：

```text
data: {"type":"start","messageId":"RESPONSE_MESSAGE_ID","messageMetadata":{"turn_id":"TURN_ID"}}

data: {"type":"text-start","id":"text-1"}

data: {"type":"text-delta","id":"text-1","delta":"你好"}

data: {"type":"text-end","id":"text-1"}

data: {"type":"finish","finishReason":"stop"}

data: {"type":"data-resume-cursor","transient":true,"data":{"frameSeq":42,"turnId":"TURN_ID"}}

data: [DONE]
```

服务端可能发送 heartbeat comment 以保持连接。

## 消息增量

文本增量输出以 `text-start` 开始，随后输出一个或多个 `text-delta`，最后
输出 `text-end`：

```text
data: {"type":"text-start","id":"text-1"}

data: {"type":"text-delta","id":"text-1","delta":"你好"}

data: {"type":"text-end","id":"text-1"}
```

所选 Agent 程序提供推理内容时，使用同样的 start/delta/end 结构：

```text
data: {"type":"reasoning-start","id":"reasoning-1"}

data: {"type":"reasoning-delta","id":"reasoning-1","delta":"正在检查代码仓库"}

data: {"type":"reasoning-end","id":"reasoning-1"}
```

同一段文本或推理内容的所有 frame 使用相同 `id`。工具输入使用
`tool-input-start`、`tool-input-delta` 和 `tool-input-available`，并通过
`toolCallId` 关联。

## 消息增量期间重连

断线重连时，将 `data-resume-cursor` 中最近一次收到的 `frameSeq` 作为
`after_seq`。具体行为取决于游标位置和消息结构：

1. **游标位于一条回复内。** Stream 会从该回复的输入消费边界开始重放，
   包含稳定的消息 ID、全部文本和工具部分，然后继续输出新的 delta。AI SDK
   恢复连接时会创建新的解析器状态，仅有游标不足以恢复此前的内容。
2. **游标位于已完成回复的边界。** 下一次响应不会重复该回复，而是等待或
   重放下一条回复。
3. **本轮已经完成，但客户端尚未收到末尾内容。** Stream 会重建最后一条
   未接收完整的回复，直到终态 frame 和游标，然后以 `[DONE]` 结束。

文本或推理块缺少对应的 `*-start` 时，AstraBox 会先补充 start frame，保证
后续 delta 符合 AI SDK 协议。客户端仍应根据各部分的 ID 处理重复数据。

内容重放不会重复发送位于请求的 `after_seq` 或更早位置的
`data-session-store-reload` 通知：即使内容读取位置回退，游标仍表示客户端已
确认该通知。收到更新的 reload 通知时，仍需重建 SessionStore 后再继续。

## 常见事件流

```text
data-turn-accepted
start
data-input-consumed
start-step
reasoning-start / reasoning-delta / reasoning-end  （可选）
text-start / text-delta / text-end
tool-input-* / tool-output-*                       （可选）
finish-step
data-result
finish
data-resume-cursor
[DONE]
```

并非每一轮都会包含全部 frame。Agent 程序可以产生多次模型调用和工具调用。
子任务状态变化时，还可能收到临时的 `data-child-runs-changed` 通知；当前状态
以 Session 的 child-run 接口为准。

### 模型结果

`data-result` 包含 Agent 程序为本轮报告的用量或费用数据。可用字段由 Agent
程序和模型服务决定，例如：

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

## 连接生命周期

- `finishReason: "stop"` 的 `finish` 表示当前回复已完成。随后还会收到持久
  游标和 `[DONE]`。如果 `messageMetadata.response_boundary === true`，平台
  turn 仍可继续处理排队输入；请查询 Session 状态，不要将连接关闭视为任务结束
- `error` 会结束当前响应。决定是否重试前，应保存它之前收到的游标并查询
  Session 状态
- `finishReason: "tool-calls"` 的 `finish` 可能只是同一个 turn 中等待交互的
  位置；回答交互请求后，本轮会在 Session stream 中继续
- 网络中断时使用 `after_seq` 重连；Session 已删除或当前用户无权访问时，
  下一次连接会返回 HTTP 错误

## 工具响应

事件流产生 `data-interaction` 或 `tool-approval-request` 时，向
`POST /api/v1/sessions/{session_id}/interaction-respond` 发送回答，并使用
交互请求中的 ID：

```json
{
  "interaction_id": "INTERACTION_ID",
  "answer": {
    "decision": "approve"
  }
}
```

回答格式由交互请求的 `presentation` 决定：表单包含问题答案，决策选择请求中
声明的 option ID，工具审批使用 `approve` 或 `reject`。完整示例见
[权限模式](permission-modes.md)。

## 事件历史

历史消息和分页请使用 messages endpoint：

```bash
curl --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/messages?limit=20" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

接口返回保存后的消息，而不是原始传输 frame；使用 `before` 向前分页。需要
继续接收原始输出时，使用 `data-resume-cursor.data.frameSeq` 和 `after_seq`：

```text
GET /api/v1/sessions/{session_id}/ai-stream?follow=session&after_seq=42
```

回复内的游标会触发完整回复重放。客户端应按稳定的消息 ID 替换或合并该回复，
不要把重放的文本直接追加到已有消息末尾。
