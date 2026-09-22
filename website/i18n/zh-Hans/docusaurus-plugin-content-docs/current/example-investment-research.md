# 创建投资研究 Agent

投资研究 Agent 可以持续运行在 AstraBox 中，收集经过授权的公司数据、执行可重复的分析，
并把报告保存到云端工作区。开发者的电脑断开连接后，任务仍可在云端继续；同一个 Agent
也可以从网页、定时计划、签名 Webhook 或消息平台启动。

| 能力 | 可以做什么 |
|---|---|
| 研究方法 | 通过提示词、Skill 或 Plugin 定义数据来源、计算、检查和输出要求。 |
| 当前数据 | 通过远程 MCP Server 或 Agent 程序支持的网页能力访问获准使用的数据源。 |
| 分析 | Agent 程序可以运行脚本，并使用 Session 工作区中的文件。 |
| 自动运行 | Deployment 可以按计划或在外部事件到达时启动 Agent。 |

## 从研究任务开始

简单的研究 Agent 可能只需要模型和清楚的提示词。只有任务需要时，再添加 MCP Server、
Skill、Plugin、凭证或更宽的网络访问范围。

打开**管理台 → Agent**，创建或编辑 Agent。研究提示词可以要求 Agent：

- 为每项重要事实注明来源和报告日期；
- 区分披露事实、自行计算和分析判断；
- 显示公式、单位、币种和期间定义；
- 说明缺失或冲突的数据；
- 将有用的表格、脚本和报告保存到工作区。

选择能够运行所需 Agent 程序并允许访问研究数据源的 Environment。Environment 决定这项
工作使用的沙箱、网络访问方式和模型连接。

## 连接研究数据源

如果获准使用的财报、行情、文档或内部服务提供远程 MCP Server，可以把它添加到 Agent。
无论 Server 定义直接填写在 Agent 中，还是由 Plugin 提供，它都是远程 MCP Server。

数据源需要凭证时，将凭证保存在 Credential Vault 中，并只为数据源的准确目标启用凭证
保护。Environment 使用受限网络时，还需要允许远程主机。Agent 只能访问管理员和用户有权
使用的数据。

具体设置见[配置 MCP Server、Plugin 和 Skill](adding-tools.md)、
[Credential Vault](credentials.md)和
[保护 Agent 使用的凭证](egress-credential-injection.md)。

## 添加可重复的研究方法

Skill 可以提供一套专门流程和配套文件；Plugin 可以把 Skill、命令和 MCP 定义打包在一起。
两者都不是必需项：没有配置它们时，所选 Agent 程序仍保留自身能力。

Git 扩展应来自经过检查的来源，并固定到检查过的 revision。研究方法可以规定如何选择可比
期间、处理重述、计算指标、引用依据，以及生成最终报告。

## 运行并检查任务

启动 Agent，并给出具体任务，例如：

> 比较最近三个财年的分部收入。每个数值都要注明财报和报告日期，解释分类变化，保存提取
> 脚本，并把结果写入 `segments.csv`。

这段对话及其工作属于一个 AstraBox Session。Agent 可以读写 `/workspace` 中的文件，网页
可以预览和下载这些文件。把脚本、中间数据和报告保存在一起，便于复现和检查结果。

## 自动运行研究任务

周期监控可以使用定时 Deployment；其他系统需要在事件发生后启动 Agent 时，可以使用签名
Webhook；用户需要从聊天中发起并接收研究结果时，可以接入消息平台。

定时计划和 Webhook 每次调用都会创建新的 Session。消息平台会启动或继续处理
对应外部会话所映射的 Session。

这些触发方式见[让 Agent 自动运行](deployments.md)。

## 检查结果

使用输出前，请检查每项引用来源、日期、计算、单位和保存的文件。确认同业和不同期间使用
相同口径，并明确处理缺失数据。生成的研究内容只用于辅助分析，不构成个性化投资建议。

## 相关指南

- [配置 Agent](authoring-agents.md)
- [配置 MCP Server、Plugin 和 Skill](adding-tools.md)
- [运行 Session](sessions.md)
- [让 Agent 自动运行](deployments.md)
