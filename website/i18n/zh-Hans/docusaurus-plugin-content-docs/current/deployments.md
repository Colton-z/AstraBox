# 让 Agent 自动运行

Deployment 将一个 Agent 连接到定时计划、签名 Webhook、外部调度器或消息平台。计划到期
或事件到达时，Agent 会自动开始工作。

定时计划和经过认证的 Webhook 会为每次调用创建新的 Session。消息平台收到消息时，会
启动或继续处理该外部会话对应的 Session。

## 选择触发方式

| 触发方式 | API scene | 可以做什么 |
|---|---|---|
| 定时计划 | `schedule` | 按照 IANA 时区中的五段式 Cron 计划运行保存的提示词。 |
| 签名 Webhook | `hmac` | 接收使用 HMAC-SHA256 对原始请求体签名的服务请求。 |
| 外部调度器 | `scheduler` | 让已有调度器使用 AstraBox 签发的密钥调用 Agent。 |
| 消息平台 | `channel:<provider>` | 让用户通过支持的聊天产品与 Agent 对话。 |

通过 API 创建 Deployment 时，使用表中对应的 `scene` 值。消息平台的 `<provider>`
取自当前部署通过官方适配器目录提供的已安装平台。可用平台随安装的适配器而定，
不是一组固定的 scene 值。

## 创建触发配置

1. 打开**管理台 → 触发配置**，选择**新建触发配置**。
2. 选择负责处理事件的 Agent。
3. 选择触发方式。
4. 填写控制台显示的定时计划、认证或消息平台信息。
5. 选择**创建**。如果 AstraBox 签发了密钥，请在离开页面前保存。

触发配置页面会显示 Agent、配置、状态和调用历史。停用后，AstraBox 不再自动调用 Agent，
但会保留配置和历史记录。

## 按计划运行

定时 Deployment 包含名称、提示词、五段式 Cron 表达式和 IANA 时区。每个到期时刻都会
使用保存的提示词创建一个新 Session。AstraBox 离线期间错过的时间不会补跑。

选择**立即运行**，可以直接执行同一项任务而不改变计划。在以前的 Run 上选择**重放**，
会使用该 Run 保存的输入创建一个新 Run。

设置方法和示例见[定时运行任务](schedules.md)。

## 接收 Webhook

签名 Webhook 使用创建 Deployment 时签发的密钥。发送方需要提供 Unix 时间戳，以及根据
时间戳和原始请求体计算的 Base64 HMAC-SHA256 签名。

访问地址、请求头和签名格式见[通过 Webhook 触发 Agent](webhooks.md)。

### 外部调度器 {#external-scheduler}

外部调度器通过 Bearer Token 或 Webhook 密钥请求头发送 AstraBox 签发的密钥。请求体会
作为 Agent 输入，并可以在最前面添加提示词前缀。

## 查看 Run 和 Session

定时 Deployment 会为计划执行、手动执行和重放分别保存一条 Run。Run 会显示任务如何
开始、当前状态，以及本次执行创建的 Session。打开 Session 可以查看对话、文件和 Agent
活动。

Session 记录实际执行的工作；Run 记录工作为什么开始，并保存“重放”使用的输入。

## 接入消息平台

消息平台触发方式同样使用 Deployment，并保存所选平台的机器人设置和凭据。AstraBox 会
认证并转换收到的平台事件，将每段外部会话映射到 Session，再通过同一平台发送 Agent 回复。

设置方法见[把 Agent 接入消息平台](channels.md)。

## 相关指南

- [运行 Session](sessions.md)
- [定时运行任务](schedules.md)
- [通过 Webhook 触发 Agent](webhooks.md)
- [把 Agent 接入消息平台](channels.md)
