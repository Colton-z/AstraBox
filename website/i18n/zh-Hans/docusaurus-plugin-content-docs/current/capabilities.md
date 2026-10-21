# 使用指引

按你的任务选择起点。Web 控制台、HTTP API、CLI 和 MCP 接口使用同一套 AstraBox 资源与访问控制。

## 选择 Agent 或 Assistant

| 你的任务 | 从哪里开始 | 保留哪些内容 |
|---|---|---|
| 给可复用的 Agent 分配任务、供团队使用，或集成到产品中 | [定义 Agent](authoring-agents.md)，然后[启动 Session](sessions.md) | Agent 定义，以及各自拥有历史和工作区的独立对话 |
| 跨多次对话持续进行个人工作 | [使用 Assistant](assistants.md) | 仅归属用户可访问的工作区、程序原生状态、记忆和对话 |
| 无需打开控制台就运行任务 | [配置 Deployment](deployments.md) | 指向 Agent 或 Assistant 的定时任务、签名 Webhook 或消息渠道绑定 |

Environment 选择已安装的 Agent 程序、沙箱、模型连接和网络设置。管理员先准备这些资源，
用户再开始工作。不可用的 Agent 仍会列出，但**开始对话**按钮会禁用；管理者可以检查它的
Agent 与 Environment 配置。

## 选择使用方式

| 你要做什么 | 指南 |
|---|---|
| 尝试自托管安装并发送第一个任务 | [快速开始](quickstart.md) |
| 在浏览器中使用流式回复、审批、工具和文件 | [Session](sessions.md)、[文件](files.md)和[语音输入](voice-input.md) |
| 保存或分享 Agent 制作的交互页面 | [Artifact](artifacts.md) |
| 添加已连接应用、MCP 服务、Skill 或 Plugin | [Agent 工具与扩展](adding-tools.md) |
| 让 Assistant 把任务委派给另一个 Agent | [Agent 间通信](a2a.md) |
| 在消息平台中接收任务结果 | [消息渠道](channels.md) |
| 周期性运行任务或由其他系统触发 | [定时任务](schedules.md)和 [Webhook](webhooks.md) |
| 在自己的应用中集成对话 | [HTTP API](api.md)和[事件流](events-stream.md) |
| 通过脚本或编程助手管理定义并提交任务 | [CLI](cli/overview.md)和 [Agent MCP](agent-mcp.md) |

控制台负责导航、输入草稿、流式输出和交互卡片。API 集成方负责自己的界面：先提交输入、
保留接收回执，再跟随输出流。接收成功表示消息已经保存；Agent 程序决定何时取走，以及由
哪条回复回答。待处理的审批或问题使用专门的交互回答接口。参阅[发送消息](sessions.md#send-a-message)。

## 回到之前的工作

离开浏览器页面不会停止 Agent。重新打开 Session，可以读取保存的消息、工具、推理和交互记录，
并继续接收新输出。**停止**用于中断当前回复，不会撤回排队输入。归档对话会使其离线，并保留历史。

沙箱确认丢失时，受影响的回复会失败。AstraBox 保留历史和原生状态，让 Agent 程序在替换后的
计算环境上恢复；不会自动重放程序已经取走的任务。原生状态恢复不依赖持久工作区卷。沙箱释放或
丢失后仍需保留任务文件时，必须配置这项可选存储。参阅 [Session 生命周期](sessions.md)和
[工作区存储](deploy.md#where-conversation-workspaces-live)。

## 运行与扩展 AstraBox

| 你的职责 | 指南 |
|---|---|
| 安装或升级部署 | [部署 AstraBox](deploy.md) |
| 离线升级时保留选定记录 | [离线数据导入](data-import.md) |
| 配置模型路由、团队登录或受保护凭证 | [模型](models.md)、[团队登录](team-login.md)和[凭证](credentials.md) |
| 规划沙箱容量和可选的文件持久化 | [Environment](environments.md) 和 [OpenSandbox](providers/opensandbox.md) |
| 把 AstraBox 嵌入 Python 应用 | [嵌入应用](embedding.md) |
| 接入程序、沙箱、身份或数据库 | [扩展接口](architecture.md#plugin-interfaces) |

内置数据库提供方使用 PostgreSQL。外部关系型提供方需要提供相应的数据库结构与协调保证；
仅有 MySQL 类型映射不代表已经支持 MySQL 部署。
