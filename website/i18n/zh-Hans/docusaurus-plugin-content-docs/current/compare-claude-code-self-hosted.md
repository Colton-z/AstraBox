# AstraBox 与 Claude Code self-hosted environments

[Claude Managed Agents](https://platform.claude.com/docs/zh-CN/managed-agents/overview) 提供预置、可配置的 Agent 运行环境及其所需基础设施。Environment 可以使用 Anthropic 管理的云端沙箱，也可以使用部署在企业基础设施中的 self-hosted environment。

AstraBox 在此基础上进一步把完整的 Agent 服务交给用户自行部署。它把已安装的 Agent 程序运行成云端 Agent，并通过 AstraBox 控制台、API、Deployment 和消息平台保持随时可用。

## 最主要的区别

Claude Code self-hosted environments 将 **Agent 执行环境**放进企业网络；AstraBox 将**完整的 Agent 平台**部署在企业管理的基础设施中。

| | Claude Code self-hosted environments | AstraBox |
| --- | --- | --- |
| 产品定位 | Claude 云端 Session 的自托管执行方式 | 开源、自托管的 Agent 平台 |
| Agent 运行方式 | Claude Code 在企业网络内的 runner 上运行 | 已安装的 Agent 程序在 OpenSandbox 沙箱中运行 |
| 控制台、API 与任务编排 | 由 Anthropic 运行 | 由 AstraBox 部署者运行 |
| Session 记录 | 由 Anthropic 保存，因此可以从支持的 Claude 入口继续 Session | 保存在部署者配置的数据库中 |
| 模型连接 | 使用 Anthropic API | 使用部署者配置的模型服务 |
| 其他系统如何使用 Agent | 通过 Anthropic 支持的产品与集成 | 通过 AstraBox API、Deployment 和消息平台 |
| 需要维护的基础设施 | runner 镜像、容量、网络和更新 | AstraBox、OpenSandbox、数据服务、Agent 镜像、容量、网络和更新 |

两种方式都可以让代码仓库、构建产物、凭证和 Agent 创建的文件留在企业基础设施中。使用 Claude Code self-hosted environments 时，对话内容——包括提示词、回复以及 MCP、命令等调用结果——仍会发送给 Anthropic，Session 记录也由 Anthropic 保存。使用 AstraBox 时，Session 记录保存在部署者的数据库中；当 Agent 调用已配置的模型服务、远程 MCP 服务或其他外部服务时，相应请求仍会离开企业网络。

## 如何选择

如果团队希望继续使用 Anthropic 的云端 Session 服务和使用入口，但需要 Claude Code 在内部代码仓库、服务与工具链附近运行，可以选择 Claude Code self-hosted environments。Anthropic 运行 Agent 服务，企业团队维护执行资源。

如果 Agent 服务本身也必须运行在企业管理的基础设施中，可以选择 AstraBox。网页控制台、API、认证、Session 记录、沙箱和集成都由企业自行运行；同一个部署还可以运行不同的受支持 Agent 程序，并连接部署者选择的模型服务。

安装 AstraBox 和创建第一个 Agent 的方法见[部署 AstraBox](deploy.md)与[快速开始](quickstart.md)。

## 与本地 Agent 程序配合使用

本地使用适合在一台电脑上交互开发；AstraBox Agent 则可以远程访问、执行长时间任务并接入其他系统，两者互补。

AstraBox 不会替代 Claude Code。它在沙箱中运行 Claude Code 和其他已安装的 Agent 程序，并提供让云端 Agent 保持随时可用的完整服务。

## 参考资料

以下 Anthropic 官方资料的核对日期为 2026-08-24：

- [Claude Managed Agents 概览](https://platform.claude.com/docs/zh-CN/managed-agents/overview)
- [Claude Code self-hosted environments](https://code.claude.com/docs/en/self-hosted-environments)
- [Run Claude Code Sessions on your own compute](https://claude.com/blog/run-claude-code-sessions-on-your-own-compute)
