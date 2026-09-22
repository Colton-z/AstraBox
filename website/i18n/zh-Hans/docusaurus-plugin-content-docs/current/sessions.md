# 启动 Session

> 创建、运行、查看和归档 Agent Session。

Session 是 Agent 的一次有状态运行。它使用一个 Agent 和 Environment，并保存
消息、Event 与当前状态。你向 Session 发送消息，它处理后返回 Event 流。

## Session 状态生命周期

Session 是一个状态机，核心状态如下：

| 状态 | 说明 | 可流转到 |
| --- | --- | --- |
| `CREATING` | 正在准备或分配运行环境。 | `READY`, `TERMINATED` |
| `READY` | 已准备好，等待用户消息。 | `BUSY`, `TERMINATED`, `RECOVERY_REQUIRED`, `DELETED` |
| `BACKGROUND_RUNNING` | 可以接收前台输入，但仍有子任务在后台执行。 | `READY`, `BUSY`, `TERMINATED`, `RECOVERY_REQUIRED`, `DELETED` |
| `BUSY` | Agent 正在处理当前前台任务。 | `INTERRUPTING`, `READY`, `RECOVERY_REQUIRED`, `TERMINATED` |
| `INTERRUPTING` | 中断指令已发出，等待当前任务停止。 | `READY`, `RECOVERY_REQUIRED` |
| `RECOVERY_REQUIRED` | 对话历史仍然保留，但运行环境需要恢复。 | `READY`, `CREATING`, `TERMINATED`, `DELETED` |
| `TERMINATED` | 运行环境已离线。 | `CREATING`, `DELETED` |
| `DELETED` | 已删除。 | —（终态） |

还有两个生命周期标记与 `state` 一起公开：

- **已归档（archived）**：与 `state` 分开记录。Session 仍可读取，但不会出现在
  活动对话列表中。
- **已删除（deleted）**：由终态 `DELETED` 表示。已删除的 Session 无法恢复。

1. <b>创建 → CREATING</b> 新 Session 进入 `CREATING`，AstraBox 正在准备
   或分配运行环境。
2. <b>CREATING → READY</b> Session 已准备好，可以接收输入。
3. <b>READY → BUSY</b> 发送消息后开始执行前台 turn。
4. <b>BUSY → READY</b> 前台 turn 完成后，Session 可以继续下一轮；仍有子任务
   执行时会显示 `BACKGROUND_RUNNING`。
5. <b>BUSY → INTERRUPTING → READY</b> 中断前台 turn 后，Session 会经过
   `INTERRUPTING`，之后仍可继续使用。
6. <b>RECOVERY_REQUIRED 或 TERMINATED</b> 对话仍然保留，但需要恢复或重建
   运行环境才能继续执行任务。
7. <b>DELETED（终态）</b> 删除 Session 后，它的生命周期结束。

恢复对话不依赖持久工作区卷。AstraBox 将 Agent 程序的原生会话数据保存到平台数据库，
更换计算资源时将这些数据恢复，交给程序自身的恢复操作使用。工作区文件单独保存：
需要在沙箱丢失或释放后保留文件时，应配置可选的持久工作区存储。连接断开本身不代表
沙箱已经失效；确认沙箱故障后，受影响的任务会结束，下一条消息可以触发恢复，不会
自动重放已经失败的任务。

## 模型请求自动重试

所选 Agent 程序或模型网关执行的重试仍属于当前任务。AstraBox 会让 Session 保持
`BUSY`，不会另外发布 `rescheduling` 状态。保持事件流连接，不要在任务执行期间重复
发送同一条消息。

事件流报告错误时，应以已记录的 turn 故障和之后的 Session 状态为准。重试分类由
所选 Agent 程序或模型网关负责；AstraBox 不会另行构造一套重试状态。

## Interrupt 语义

- **对 `READY` Session 调用 interrupt**：幂等的空操作，Session 保持可用。
- **对有活动 turn 的 Session 调用 interrupt**：AstraBox 记录请求，让 Session
  经过 `INTERRUPTING`，并返回 `status: "accepted"`。
- **interrupt 后**：turn 结束后，Session 仍可复用。

```bash
# 中断当前任务
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/interrupt" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

只有 `DELETED` 是终态。Interrupt 前台 turn 不会删除 Session 或已保存的对话历史。

## 向正在处理 turn 的 Session 发消息（409 错误）

Session 已有活动前台任务或仍在处理中断时，`POST
/api/v1/sessions/{session_id}/ai-stream` 返回 `HTTP 409 SESSION_BUSY`。等待状态变为
`READY` 或 `BACKGROUND_RUNNING`，或者先中断当前任务，再发送下一条前台消息。

```json
{
  "code": "SESSION_BUSY",
  "message": "session already has an active turn",
  "data": null,
  "error": {
    "code": "SESSION_BUSY",
    "status_code": 409,
    "category": "state",
    "retryable": true,
    "owner": "session",
    "user_message": "session already has an active turn"
  }
}
```

发送下一条消息前，应等待当前 turn 的终止 frame。存在待处理交互时，应改用交互响应
接口回答。

## 创建 Session

Session 从 Agent 启动，Environment 已经由这个 Agent 选定。在控制台中：

1. 在 AstraBox 控制台中打开 **Agents**。
2. 选择一个 Agent。
3. 选择 **开始对话**。

运行环境准备好后，新的 Session 会自动打开。Agent 开启预热时，Session 可以领取
事先准备完成的运行实例，其中已包含 Agent 配置和所选扩展。配置持久工作区存储后，
AstraBox 会在交付运行实例前选定新 Session 的工作区，或已有 Session 保存的工作区。
运行实例使用期间，这个分配关系保持固定。

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
| `current_turn_id` | string/null | 当前 turn 的 ID。 |
| `last_turn_status` | string/null | 最近一次 turn 的状态。 |
| `created_at` | string/null | 创建时间。 |
| `updated_at` | string/null | 最后更新时间。 |

Session 列表只返回摘要；`GET /api/v1/sessions/{session_id}` 返回当前完整详情。
Agent 程序在 turn 完成时报告 token 用量或模型费用后，控制台会把这些数据和结果
一起显示。计费和限额由当前部署配置的模型服务管理。

AstraBox 创建 Session 时不接受 Agent 对象、Agent 版本或 Environment 请求字段。
请求路径指定 Agent，而 Environment 已经由 Agent 选定。Session 记录 Agent 身份，
不会快照或锁定某个 Agent 版本：

- AstraBox 第一次为 Session 准备运行环境时，解析当前 Agent 和 Environment
- 保存 Agent 或 Environment 后，不会原地修改正在执行的 turn 或已经运行的环境
- AstraBox 后续重建运行环境时，同一个 Session 可能使用当前保存的 Agent 和 Environment
  配置
- Agent 的 `version` 字段用于检测并发更新，不是 Session 可以选择的配置版本

## 创建后添加资源

AstraBox 创建 Session 的请求不接受 `resources` 数组。默认 GitHub 仓库和 Vault
分配关系配置在 Agent 上；一次性文件则在 Session 运行环境准备好后直接上传到工作区。
参阅[访问 GitHub](working-with-repos.md)、[使用 Vault 认证](credentials.md)和
[上传与下载文件](files.md)。

## 发送消息

`POST /sessions/{id}/ai-stream` 的请求体包含一条用户消息，并以 AI SDK UI
Message Stream v1 事件流返回本轮结果。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `content` | string | 是 | 用户消息。 |
| `client_message_id` | string | 否 | 调用方为这条消息生成的幂等 key。 |
| `permission_mode` | string | 否 | 这个 Session 的 Agent 程序支持的权限模式。 |

```bash
# 发送消息并以事件流接收响应
curl --no-buffer --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/ai-stream" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{
    "content": "分析当前目录下所有 Python 文件的代码复杂度。",
    "client_message_id": "analysis-1"
  }'
```

发送消息会让 `READY` 状态变为 `BUSY`；处于 `BACKGROUND_RUNNING` 的 Session
也可以接收新的前台 turn。处理完成后，Session 回到 `READY`；仍有子任务执行时
则显示 `BACKGROUND_RUNNING`。当前前台 turn 进入终态后，事件流以
`data: [DONE]` 结束。

## 读取事件

`ai-stream` 响应使用 AI SDK UI Message Stream 协议。以下连接不发送输入，而是等待并
接收一个完整任务：

```text
GET /api/v1/sessions/{session_id}/ai-stream?follow=session
```

`data-resume-cursor` frame 的 `data.frameSeq` 带有最新的安全整数游标。保存该值，
断线后通过 `after_seq` 重连；AstraBox 不使用 SSE `Last-Event-ID` 请求头：

```text
GET /api/v1/sessions/{session_id}/ai-stream?follow=session&after_seq=42
```

通过 `GET /api/v1/sessions/{session_id}/messages` 读取持久保存的对话历史，并使用
`before` 向前分页。Frame 格式和断线重连方式见 [SSE Event 流](events-stream.md)。

## 读取和更新 Sessions

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

分页响应：

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
控制台使用的非分页数组。这个 Session 路由族只允许更新权限模式；具体可选值由
所选 Agent 程序定义。

## Child runs

Agent 程序可以把工作委派给 child run。AstraBox 通过 Session 级接口公开它们的树形
关系、引擎原生状态、消息、用量摘要和当前支持的操作：

```text
GET /api/v1/sessions/{session_id}/child-runs
GET /api/v1/sessions/{session_id}/child-runs/{child_run_id}/messages
POST /api/v1/sessions/{session_id}/child-runs/{child_run_id}/stop
```

控制台中的 **Agents** 标签展示同一个投影。委派方式、共享工作区和控制操作见
[Multiagent 编排](multi-agents.md)。

## 生命周期

Session 不再需要出现在活动对话列表中、但仍应保留读取能力时，使用归档：

```text
POST /api/v1/sessions/{session_id}/archive
```

需要让记录和运行环境进入终态 `DELETED` 时，删除 Session：

```text
DELETE /api/v1/sessions/{session_id}
```

结束或删除 Session 与中断当前任务不同。应根据需要保留的数据选择对应操作。

## 多轮对话工作流

Session 支持多轮对话。推荐流程如下：

1. 通过 `POST /sessions/{id}/ai-stream` 发送用户消息
2. 接收 AI SDK UI Message Stream 响应
3. 等待终止 frame，并确认 Session 进入 `READY` 或 `BACKGROUND_RUNNING`
4. 向同一个 Session 发送下一条消息

```bash
BASE_URL="$SERVICE_URL/api/v1"

# 第一轮：提出需求
curl --no-buffer --silent --show-error --request POST \
  "$BASE_URL/sessions/$SESSION_ID/ai-stream" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"content":"创建一个 Python Flask 项目脚手架。","client_message_id":"turn-1"}'

# 第二轮：追加要求
curl --no-buffer --silent --show-error --request POST \
  "$BASE_URL/sessions/$SESSION_ID/ai-stream" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"content":"给项目添加单元测试和 CI 配置。","client_message_id":"turn-2"}'
```

> 发送下一条消息前，应等待当前前台 turn 结束。Session 仍处于 `BUSY` 或
> `INTERRUPTING` 时发送消息会返回 HTTP 409。

## 分享 Session

打开 Session 并选择**分享**，即可生成只读链接。可以设置有效期，并决定查看者是否能够
下载工作区文件。有效链接本身授予对会话的读取权限，因此只应发送给目标查看者，并在
不再需要时撤销。查看者不能发送消息或修改 Session。

## 最佳实践

1. **把 Agent 配置视为实时配置** — Session 不锁定 Agent 版本。保存 Agent 不会
   修改正在运行的 turn，但运行环境重建后会使用当前保存的 Agent 和 Environment
   配置。
2. **使用稳定的请求标识** — 为每个 Session 创建请求设置稳定的
   `Idempotency-Key`，并为每条消息设置稳定的 `client_message_id`，以避免重复请求
   并便于追踪。
3. **明确区分中断和归档** — 需要停止前台 turn 时使用 interrupt；只有在 Session
   应离开活动列表并释放运行环境时才归档。

## 常见问题

**Q：向 `BUSY` 状态的 Session 发消息会怎样？**

A：`POST /sessions/{id}/ai-stream` 会返回 `HTTP 409 SESSION_BUSY`。请等待
当前前台任务结束，或先中断它。需要分别提交输入和接收输出的外部系统可以使用
`POST /sessions/{id}/turn-inputs`；输入能否加入正在执行的任务，由所选 Agent
程序决定。

**Q：Interrupt 后还能继续使用 Session 吗？**

A：可以。活动 turn 结束后，发送下一条消息即可继续。Interrupt turn 不会删除
Session 或它的历史记录。

**Q：如何获取 Session 的完整对话历史？**

A：通过 `GET /sessions/{id}/messages` 获取，并使用 `before` 向前分页。当前
任务仍在执行时，第一页还会包含尚未保存完成的任务内容。

**Q：SSE 断线后如何重连？**

A：保存最新 `data-resume-cursor` frame 的 `data.frameSeq`，然后使用 `after_seq`
查询参数重连。AstraBox 不读取 `Last-Event-ID`。

**Q：创建或编辑 Agent 时为什么找不到某个 Environment？**

A：Agent 表单只列出已启用、并且其 Agent 程序支持 Agent 的 Environment。
**运行环境**管理页也会显示已停用项；请在那里检查 Environment 的启用状态和
所选 Agent 程序。

## API Reference

- 当前部署实例的 `/docs` 与 `/openapi.json` 参考
- [SSE Event 流](events-stream.md) — 接收输出并安全地断线重连
- [Multiagent 编排](multi-agents.md) — 查看和控制 child run
- [上传与下载文件](files.md) — 使用 Session 工作区
