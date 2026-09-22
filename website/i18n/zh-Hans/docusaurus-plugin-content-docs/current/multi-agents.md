# Multiagent 编排

> 让 Agent 把工作委派给子任务，并从父 Session 查看各项活动。

Multiagent 编排允许 Session 中的 Agent 程序把工作拆分为多个 child run。每个 child
run 都有独立的引擎会话，父 Agent 汇总各项结果并向用户返回最终答案。

适用于可以按职责拆分、并行或分阶段执行的复杂任务。对于一步即可完成、必须严格串行，
或多个执行者会频繁修改同一文件的任务，建议使用单 Agent，流程会更简单。

## 工作原理

已安装的 Agent 程序负责委派。它决定是否启动 child run、传入哪些上下文，以及是否向
已完成的 child run 追加任务。AstraBox 把引擎原生的运行记录投影为 Session 级视图，
不会把它们重新定义为另一套 Agent 名单或 Thread 模型。

| 概念 | 说明 |
| --- | --- |
| 父 Agent | 处理当前 Session 的 Agent。它可以拆分任务、跟进子任务结果并生成最终答案。 |
| Child run | 一次被委派的引擎会话，由 `child_run_id` 标识，包含自己的消息、状态和可选用量摘要。 |
| Child-run 树 | Agent 程序报告的父子关系。程序支持嵌套委派时，`depth` 和 `parent_child_run_id` 会保留该关系。 |
| 可用操作 | 当前 Agent 程序为 child run 提供的操作。AstraBox 通过 `operations` 发布，不根据程序名称推断。 |

各类资源和上下文的作用范围如下：

| 范围 | 行为 |
| --- | --- |
| Environment 和文件系统 | Child run 在父 Session 的运行环境中执行，共享 Environment、沙箱和工作区。 |
| Vault | Child run 使用父 Agent 运行环境中可用的凭证。 |
| 会话记录 | 每个 child run 都有自己的引擎原生消息，只接收父任务传给它的上下文。 |
| Agent 配置 | 委派行为由已安装的 Agent 程序和 Session 运行环境解析到的 Agent 配置决定。 |
| Event 流 | 父 Session 流通过 `data-child-runs-changed` 通知客户端刷新 child-run 视图；child-run 接口返回单个子任务的完整投影消息。 |

:::warning
并行 child run 共享同一个文件系统。应在系统提示词中划分清晰的文件或目录职责，避免多个
child run 同时修改同一文件。
:::

## 适合委派的任务

根据任务依赖和职责边界设计系统提示词：

- 彼此独立的调研、模块实现或数据收集工作，可以交给不同 child run 并行执行。
- 按职责拆分实现、测试和评审。例如，一个 child run 可以实现改动，另一个只读结果并
  返回问题清单。
- 有前后依赖的任务按阶段执行，例如先实现、再评审；父 Agent 根据评审结果决定是否继续
  迭代。

系统提示词还应规定各 child run 的输出格式，并指出必须由父 Agent 自己处理的工作。

## 配置父 Agent

### 通过控制台配置

1. 打开 **控制台 → Agent**，创建或编辑一个已安装 Agent 程序支持委派的 Agent。
2. 在系统提示词中说明任务拆分、委派条件、交付格式和冲突处理方式。
3. 只添加任务需要的 MCP 服务器、Skill、Plugin 和仓库访问。
4. 保存 Agent，然后启动 Session。

AstraBox 不定义 `multiagent.agents` 名单。委派工具和 child-run 语义属于所选 Agent
程序。Session 中的 **Agents** 标签始终可见；程序启动 child run 之前，其中会显示
空状态。

### 通过 API 配置

使用普通的 Agent 创建或更新 API。无需创建第二个 Coordinator 资源，也没有平台自定义
的子 Agent 配置。通过 `GET /api/v1/agent-configuration/schema` 读取所选 Agent 程序实际
消费的输入，再按[定义 Agent](authoring-agents.md)配置系统提示词和扩展。

### Agent 配置与 Session 运行环境

Session 不会冻结可选择的 Agent 版本或 Agent 名单快照。AstraBox 在准备或重建 Session
运行环境时解析当前 Agent 和 Environment。保存 Agent 不会原地改写正在运行的任务；
之后再次准备运行环境时，可能使用当前保存的配置。

## 创建并运行 Session

从父 Agent 创建 Session：

```bash
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/agents/$AGENT_ID/conversations" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{}'
```

发送一个写明职责边界的任务：

```bash
curl --no-buffer --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/ai-stream" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{
    "content": "分析登录模块的实现和安全风险。适合时把相互独立的实现与评审工作委派出去，最后给出修复建议。",
    "client_message_id": "login-review-1"
  }'
```

提示词不会强制产生委派。Agent 程序会判断 child run 是否有用，并决定怎样执行。

## 连接 MCP Server 和 Vault

MCP 服务器、Skill、Plugin、仓库访问和 Vault 配置在父 Agent 或其 Environment 上：

- Child run 使用同一个 Session 运行环境中可用的功能。
- Environment 的网络策略同时作用于父任务和子任务。
- Vault 凭证沿用父 Session 的 Agent 或 Assistant 分配关系和出站保护规则。
- 只有被委派的工作确实需要时，才授予高权限凭证。

参阅 [Agent 工具与扩展](adding-tools.md)、[Vault](credentials.md)和
[权限模式](permission-modes.md)。

## 观察 child run 和 Event

打开 Session 并选择 **Agents** 标签。这里会显示 child-run 树、当前状态、描述、摘要，
以及 Agent 程序报告的用量。选择一项即可打开它的消息和工具活动。

按树形顺序列出所有 child run：

```text
GET /api/v1/sessions/{session_id}/child-runs
```

```bash
curl --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/child-runs" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

读取一个 child run 的投影消息：

```text
GET /api/v1/sessions/{session_id}/child-runs/{child_run_id}/messages
```

```bash
curl --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/child-runs/$CHILD_RUN_ID/messages" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

Session 流中的 `data-child-runs-changed` 只是刷新信号。应通过 child-run 接口读取当前树和
会话记录，不要根据瞬时通知自行重建状态。

## 中断单个 child run

只有所选 Agent 程序为当前 child run 提供有效控制方式时，`operations` 数组才包含
`stop`。通过这个已发布的操作请求停止：

```text
POST /api/v1/sessions/{session_id}/child-runs/{child_run_id}/stop
```

```bash
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/child-runs/$CHILD_RUN_ID/stop" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

请求成功时返回 `status: "accepted"`。已关闭的 child run 返回
`CHILD_RUN_ALREADY_TERMINAL`；正在运行但不支持停止的任务返回
`CHILD_RUN_CONTROL_UNAVAILABLE`。

## 工具权限和交互

Agent 程序执行父任务和子任务时，都使用 Session 的引擎原生权限模式。程序请求确认或
其他输入时，AstraBox 会在父 Session 中发布该交互，并通过同一个引擎适配器返回答案。

将 interaction ID 发送到 `POST
/api/v1/sessions/{session_id}/interaction-respond`。待处理交互表示当前工作正在等待输入，
不代表 child run 已完成。响应格式见[权限模式](permission-modes.md)。

## 限制

| 项目 | 行为或限制 |
| --- | --- |
| Agent 名单 | AstraBox 没有平台级子 Agent 名单；委派由已安装的 Agent 程序负责。 |
| Child run 数量 | 数量和并发量由 Agent 程序及其当前配置决定。 |
| 委派层级 | 是否支持嵌套委派由 Agent 程序决定；`depth` 和 `parent_child_run_id` 报告最终树形关系。 |
| Session 前台状态 | 子任务仍在执行时，Session 可以显示 `BACKGROUND_RUNNING` 并继续接收前台输入。 |
| Agent 引用 | Child run 是引擎会话，不引用独立的 AstraBox Agent 记录或版本。 |
| 控制方式 | 以每一项的 `operations` 数组为准；AstraBox 不要求或推断统一的委派工具集。 |

## 常见问题

### Agent 没有委派任务

确认已安装的 Agent 程序支持 child run，并且系统提示词定义了可以拆分的工作。没有
child run 的 Session 也是有效状态，**Agents** 标签会显示空状态。

### 更新 Agent 后 Session 未使用新配置

保存 Agent 不会重新配置已经在执行的任务或运行环境。新 Session 会使用已保存的配置；
AstraBox 后续重建已有 Session 的运行环境后，该 Session 也可能使用这份配置。

### 创建或更新父 Agent 返回错误

读取 `GET /api/v1/agent-configuration/schema`，并根据校验错误定位被拒绝的字段。不要添加
平台级 `multiagent.agents` 名单；委派专用配置由所选 Agent 程序声明。Agent
`version` 过期时则会返回 `409 AGENT_VERSION_CONFLICT`，应读取当前 Agent 后再重试。

## 相关文档

- [定义 Agent](authoring-agents.md)
- [启动 Session](sessions.md)
- [SSE Event 流](events-stream.md)
- [权限模式](permission-modes.md)
- 当前部署实例的 `/docs` 与 `/openapi.json` API 参考
