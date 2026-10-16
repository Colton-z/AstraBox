# 消息平台回应判断器

`attention_policy` 为 `participated` 或 `proactive` 的消息平台 Deployment，会向回应
判断器询问：对于没有人专门发给 Agent 的普通群消息，Agent 是否应该回应。核心负责决定哪些
消息成为候选，把策略随每条消息固定下来，收集话题的上下文，记录判断结果并在需要时恢复；
判断器只针对一条消息回答一个问题。

## 哪些消息需要判断

私信、提及 Agent 或回复 Agent 的消息无需判断器，直接回应。普通消息按以下方式判断：

| 策略 | Agent 尚未回应过的话题 | Agent 已回应过的话题 |
|---|---|---|
| `participated` | 保留为上下文，不询问判断器 | 判断 |
| `proactive` | 判断 | 判断 |

话题是消息所属的对话：它所在的群组或主题帖，或它延续的回复链。“已回应”指自
Deployment 最近一次编辑以来，Agent 回应过该话题中的消息。策略和判断指令随每条消息固定
下来，因此编辑不会改变已记录消息的判断方式。

每条消息最多判断一次。判断器决定不回应的消息不会创建对话，并作为上下文，留给 Agent 下一次
回应的那条消息。重新投递的副本读取已记录的判断结果。判断器出错时会退避重试，错误绝不会被
当作判断结果。同一条消息的某个副本如果提及了 Agent，即使判断器决定不回应，这条消息
也会执行。平台只作为上下文报告的消息（`observation` 信号）从不判断：平台没有授权回应它。

## 内置判断器

未安装判断器时，如果启用了标题模型（`ASTRABOX_TITLE_MODEL_ENABLED`），AstraBox 会使用
自带的判断器。它把这条消息、该话题最近 50 条尚未提交的消息（每条截断为 2000 个字符）、
Agent 是否已在该话题中回应过，以及 Deployment 的 `response_instruction` 发送给标题模型，
并要求模型准确返回 `{"respond": boolean, "reason": string}`。默认保持沉默；判断指令
说明 Agent 何时应该发言。其他任何输出都视为错误，而不是判断结果。

在控制台或 API 中为 Deployment 设置策略和判断指令：

```json
{"attention_policy": "proactive",
 "response_instruction": "回答有关部署和故障的问题；其他情况保持沉默。"}
```

`response_instruction`（最多 4000 个字符）只适用于这两种策略；在其他策略下保存会被
拒绝，把策略改为其他策略时会清除它。既没有标题模型、也没有安装判断器时，保存这两种
策略中的任意一种都会以 `CHANNEL_RESPONSE_JUDGE_UNAVAILABLE` 被拒绝。

## 安装判断器

一个部署最多安装一个判断器，它会取代内置判断器：

```toml
[project.entry-points."astrabox.providers.channel_response"]
enterprise = "my_deployment.judge:ResponseJudge"
```

入口点指向的类继承 `astrabox.seams.channel_response.ChannelResponseJudge`，带有稳定的
`name`，并实现异步方法 `judge(*, context)`，返回
`ChannelResponseDecision(respond, reason)`。

| `ChannelResponseContext` 字段 | 含义 |
| --- | --- |
| `deployment_id`、`channel_name` | 触发配置及其 provider |
| `conversation` | 话题：核心解析出的对话键或回复链 |
| `message` | 候选消息的参与者、文本和原始时间戳 |
| `policy`、`instruction`、`binding_revision` | 在消息首次记录时固定 |
| `selection_reason` | `participated_topic` 或 `proactive_policy` |
| `participated` | 该话题在此修订版本下是否已有消息得到回应 |
| `context` | 该话题中位于这条消息之前、尚未提交的消息，按时间从早到晚排列 |

宿主故障后，同一条消息可能被再次判断，因此判断器必须没有副作用：不要发送消息、写入
记录、分派任务，也不要发起授权同意流程。无法做出判断时应抛出异常；核心会退避重试，
绝不会把异常当作 `respond=true`。判断理由会保存在消息记录中，不得包含机密信息。每次
调用判断器的时限为 30 秒。
