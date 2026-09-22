# 管理 Schedule

> 按照定时计划自动运行 Agent。

在 AstraBox 中，Schedule 是一种触发配置，它把 Agent、时间规则和提示词关联
起来。每个到期时刻都会启动一个新的 Agent Session，无需保持已有 Session
在线。

可以在 Web 控制台中创建、查询、修改、停用、立即运行、重放和删除 Schedule。

## 使用前提

- 创建负责处理每次任务的 Agent。
- 以 Agent 所有者或 AstraBox 管理员身份登录。
- AstraBox 数据库使用 PostgreSQL 或 SQLite；其他数据库后端不支持定时计划。

打开**控制台 → 触发配置 → 新建触发配置**，然后选择**定时计划**。表单中包含
完整的 Schedule 配置：

| 字段 | 说明 |
| --- | --- |
| Agent | 每次触发后负责处理任务的 Agent。 |
| 名称 | 用于区分不同 Schedule 的简短名称。 |
| 提示词 | 每次计划触发时，希望 Agent 完成的任务。 |
| Cron 表达式 | 五段 Cron 表达式：分钟、小时、日期、月份和星期。 |
| 时区 | 有效的 IANA 时区，例如 `Asia/Shanghai`、`America/Los_Angeles` 或 `UTC`。 |

表单会优先填入浏览器检测到的 IANA 时区，无法检测时使用 `UTC`。创建前请确认
时区是否正确。

## 创建 Schedule

在表单中说明以下信息：

- 执行时间，使用五段 Cron 表达式。
- 任务内容。
- 在提示词中写明输出要求。
- 时区。

### 周期任务

按上海时间每个工作日上午 9 点运行：

```text
Cron 表达式：0 9 * * 1-5
时区：Asia/Shanghai
提示词：汇总过去 24 小时的 AI 行业新闻，选出最重要的 5 条，并附上来源链接。
```

按洛杉矶时间每周一上午 10 点运行：

```text
Cron 表达式：0 10 * * 1
时区：America/Los_Angeles
提示词：整理上周的项目进展、风险和待办事项，按 Markdown 表格输出。
```

### 单次任务

内置 Schedule 是周期任务，不提供单次触发。对于单次任务，请创建外部调度器
触发配置，并让调度器在指定时间调用 AstraBox。详见[自动运行
Agent](deployments.md#external-scheduler)。

### 固定间隔任务

五段 Cron 可以表示按分钟、小时、天、周或月执行的周期。例如，每 30 分钟
运行一次：

```text
Cron 表达式：*/30 * * * *
时区：UTC
```

无法用五段 Cron 表示的间隔或日期范围，请使用外部调度器及其带认证的触发地址。

创建完成后，Schedule 详情页会显示名称、Agent、提示词、Cron 表达式、时区、
状态和运行记录。选择**停用**可以停止后续定时触发，同时保留配置。

## 查询 Schedule

打开**控制台 → 触发配置**，查看当前用户有权管理的 Agent 触发配置。可以按
Agent、Schedule 名称、触发方式或 ID 搜索，也可以按启用或停用状态筛选。

打开 Schedule 后，可以查看或修改提示词和时间规则，启用或停用 Schedule、
立即运行，以及查看运行记录。

## 删除 Schedule

打开 Schedule 详情页并选择**删除**。删除后不再触发新任务，该触发配置也会
从列表中移除；每次已完成运行创建的 Session 仍是独立的 Session 记录。

如果以后还需要恢复配置或查看运行记录，请改用**停用**。停用后的 Schedule
不会自动运行，之后可以重新启用。

## 查看执行结果

- 每次定时触发都会创建一条运行记录和一个新的 Session。
- 选择**立即运行**，无需修改时间规则即可启动一次新任务。
- 在历史运行记录上选择**重放**，会使用该次运行保存的提示词创建另一条运行
  记录。
- 打开包含 `session_id` 的运行记录，可以查看 Session 的消息、文件和状态。

运行记录会标明本次任务来自定时计划、**立即运行**还是**重放**。不同运行记录
之间不共享对话上下文。

## 使用限制

- 内置 Schedule 是周期任务，只接受五段 Cron 和有效的 IANA 时区，最小粒度
  为 1 分钟。
- 定时计划要求使用 PostgreSQL 或 SQLite；每个 AstraBox 安装最多启用 64 个
  Schedule。
- AstraBox 离线期间错过的时刻不会补跑。Schedule 也不能修改成其他触发方式；
  需要时请创建新的触发配置。

## 相关文档

- [自动运行 Agent](deployments.md)
- [运行 Session](sessions.md)
- [通过 Webhook 触发 Agent](webhooks.md)
- [把 Agent 连接到消息平台](channels.md)
