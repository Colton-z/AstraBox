# Assistant

Assistant 是只属于一个用户的个人云端工作区。多次对话共享同一个工作区，Agent 程序的
原生状态由 AstraBox 保存到平台数据库。沙箱释放或丢失后需要保留工作区文件时，部署方
应配置可选的持久工作区卷。

持续整理笔记、分析不断变化的文件，或分几天完成同一个项目，都适合使用 Assistant。需要
让用户或其他系统通过管理台、API、Deployment 或远程 MCP 使用可复用的云端 Agent 时，
应创建 [Agent](authoring-agents.md)。

## Agent 与 Assistant

| | Agent | Assistant |
|---|---|---|
| 谁可以使用 | 通过 Agent 鉴权的账户 | 归属用户 |
| 典型用途 | 重复执行的任务、团队使用和自动化 | 一个人持续进行的工作 |
| 工作区 | 每次对话使用独立工作区 | 归属用户的多次对话使用同一工作区 |
| 启动方式 | 管理台、API、Deployment 或远程 MCP | 管理台或 API |

Assistant 不会把多次对话合并为一段对话。每次对话都有独立的 Session 记录和历史，但这些
Session 会在同一个 Assistant 工作区中运行。

## 创建 Assistant

Assistant 需要选择一个 **Environment（运行环境）**。Environment 是一套保存好的运行
设置，包括 Agent 程序、沙箱设置、模型连接和网络访问。Assistant 创建页面只显示已启用
且支持 Assistant 的 Environment。

1. 打开**管理台 → Assistant**，选择**创建 Assistant**。
2. 填写名称并选择 **Environment**。
3. 根据需要填写说明，并选择默认[权限模式](permission-modes.md)。
4. 选择**创建**。

当前登录用户会成为归属用户。Environment 同时决定 Assistant 使用的 Agent 程序；创建后，
管理台不会再提供这两项的修改入口。名称、说明、图标和默认权限模式仍可编辑。

## 开始并继续工作

在用户界面中打开 **Assistant**，选择一个 Assistant，再选择**开始对话**。AstraBox 会按需
准备或恢复工作区，并创建一个新的 Session。以后再次开始对话时，会创建另一个 Session，
但仍然使用同一套工作区。

Agent 程序定义自身的原生状态，AstraBox 将其保存到平台数据库以供恢复。每段对话也有
独立的 Session 历史。结束一段对话不会结束 Assistant 或清空共享工作区；沙箱更换后
是否保留工作区文件，取决于是否配置持久工作区存储。

消息、审批、问题、文件和分享链接都使用 [Session](sessions.md) 中介绍的统一界面。

## 暂停与恢复工作区

Assistant 不需要占用计算资源时，可以在详情页选择**暂停工作区**。AstraBox 会停止
原生状态写入，确认 Agent 程序状态已保存到平台数据库，然后释放沙箱。此操作不依赖
持久工作区卷。需要跨暂停保留工作区文件时，应配置持久卷，或先导出文件。

选择**恢复工作区**或直接开始新的对话，即可把保存的原生状态恢复到一个新沙箱中。
配置持久工作区存储后，同一个 Assistant 的文件会挂载到新沙箱；未配置时，新沙箱使用
新的文件系统。Assistant 的这项操作会释放并重新分配计算资源，不调用 OpenSandbox
的沙箱暂停与恢复操作。

管理台会显示工作区处于未启动、启动中、就绪、已暂停或需要处理状态。只有保存完成并确认
沙箱已经释放后，暂停操作才会报告成功。任一步骤无法确认时，Assistant 会保留，以便重试。

## 鉴权与凭证

只有归属用户可以查看、打开、修改、暂停、恢复或删除 Assistant。其他账户访问时，会收到
与 Assistant 不存在时相同的未找到响应。

凭证传递与归属鉴权相互独立。使用已分配的 MCP 凭证需要开启沙箱凭证保护。沙箱后端
只会在匹配的出站请求中加入凭证，保存的值留在 Assistant 沙箱之外。关闭凭证保护时，
需要认证的受管理 MCP 配置会被拒绝。

管理员可以在**管理台 → 凭证**中为 Assistant 分配 Credential Vault（凭证库）。Assistant
可以使用其中分配的 MCP 凭证，但用户和 Agent 程序都看不到保存的凭证值。支持的凭证类型
和分配规则见[凭证](credentials.md)。

## 删除 Assistant

删除前，请先导出需要另外保存的文件。只有确认当前沙箱已经销毁后，AstraBox 才会移除
Assistant 记录。如果无法确认销毁，记录会继续保留，用户可以重试删除。

## 通过 API 使用

创建 Assistant、开始对话、管理工作区生命周期和删除 Assistant 也可以通过 HTTP API
完成。每个 AstraBox 实例都在 `/docs` 提供当前版本的交互式 API 参考，并在
`/openapi.json` 提供 OpenAPI 文档。鉴权和请求约定见 [API 概览](api.md)。

## 相关指南

- [Environment](environments.md)
- [Session](sessions.md)
- [凭证](credentials.md)
- [连接模型服务](models.md)
