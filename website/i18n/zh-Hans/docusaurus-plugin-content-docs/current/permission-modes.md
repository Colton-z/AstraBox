# 权限模式

> 控制所选 Agent 程序如何处理工具调用和审批。

权限模式控制 Agent 可以访问什么，以及在执行工具操作前**是否需要人工审批**，让你在 Agent 自主执行和人工监管之间找到平衡。

已安装的 Agent 程序采用的权限模型并不相同。AstraBox 保留每个程序原有的模式名称和行为，不会把它们改写成平台自定义的 `allow / ask / deny` 策略。

## 运行时行为

打开 Agent 对话，使用消息输入框下方的权限选择器。只有当前 Agent 程序支持切换模式时，页面才会显示这个选择器。

选择器只会显示 Agent 所用 RuntimeProfile 中 Agent 程序声明的模式；名称和说明均来自该程序。未选择模式时使用的默认值也由程序声明。AstraBox 不会解析命令来覆盖程序的决定，也不会重新解释程序的审批请求。有些程序还会在 Agent 的**高级设置**中提供单独的审批选项，它与权限模式选择器是两项不同的设置。

权限模式不会绕过 RuntimeProfile 的网络策略或沙箱隔离设置。

## 为 Session 配置模式

打开 Agent，开始或继续一段对话，然后在消息输入框下方选择权限模式。Agent 空闲时可以切换；你的下一条消息会在新的模式下运行，无需修改 Agent，也无需另开一段对话。

如果所选 Agent 程序还提供单独的审批选项，请在创建或编辑 Agent 时进入**高级设置**配置。控制台会把它和对话中的权限模式分开显示。

## 人工确认流程

Agent 在操作前需要审批时：

1. Agent 产生一项审批请求
2. Agent 的回复暂停，等待人工输入
3. 网页控制台显示操作内容、参数和可选项
4. 用户批准或拒绝这项操作
5. Agent 根据决定继续同一条回复

关闭浏览器不会批准或拒绝这项操作。只要请求仍在等待处理，重新打开同一段对话就可以继续。

## 确认或拒绝操作

网页控制台会直接处理审批请求。API 客户端也可以通过以下接口回答同一项请求：

```text
POST /api/v1/sessions/{session_id}/interactions/{interaction_id}/answer
```

使用待处理请求的 `interaction_id`。`GET /api/v1/sessions/{session_id}` 会在 `pending_interaction` 中返回这项请求，Session 事件流也会通过 `data-interaction` part 发送它。

请求的 `presentation` 决定 `answer` 的格式：

| `presentation` | `answer` |
|---|---|
| `tool_approval` | `{"decision": "approve"}` 或 `{"decision": "reject"}` |
| `decision` | `{"decision": "<id>"}`，其中 `<id>` 是请求中某个 `options` 的 `id` |
| `form` | `{"answers": [...]}`，请求中的每个 `questions` 对应一项；或 `{"decline": true}` |

每个 `answers` 条目写明它的 `question_id`，并携带 `option_label`；多选问题使用 `option_labels`；问题允许时也可以使用 `free_text`。

批准 `tool_approval` 请求的请求体：

```json
{
  "answer": {
    "decision": "approve"
  }
}
```

或拒绝它：

```json
{
  "answer": {
    "decision": "reject",
    "comment": "该操作不在本次任务范围内"
  }
}
```

拒绝时，如果 Agent 可以根据原因调整方案并重试，请附带 `comment`。

## 常见问题

**Q：一条回复可以需要多次回答吗？**

A：可以，但 AstraBox 同一时间只公开一项活动交互。这一项处理完成后，Agent 程序可以在同一条回复中请求下一项回答。

**Q：待确认操作有超时吗？**

A：超时不由浏览器决定。Agent 程序可以结束自己的等待，运行配置丢失也会结束这项请求。发生这种情况后，回答旧请求会返回 `409 INTERACTION_NOT_OPEN`；发送下一条消息即可继续对话。

**Q：客户端自定义工具可以使用权限模式吗？**

A：AstraBox 没有定义平台级的客户端自定义工具接口。如果所选 Agent 程序需要客户端输入，AstraBox 会传递该程序原生的交互，并通过交互的 `answer` 接口返回回答；这个回答如何影响工具执行，由该程序决定。

## 下一步

- [Agent 工具与扩展](adding-tools.md) — 配置 Agent 可用的能力。
- [SSE 事件流](events-stream.md) — 接收输出和待处理交互。
- [Session](sessions.md) — 启动并继续 Agent 工作。
- [定义 Agent](authoring-agents.md) — 查看 Agent 配置。
