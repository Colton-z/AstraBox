# 使用指引

一分钟内选对 AstraBox 的使用方式——面向新用户的入门概览。

## 关于 AstraBox

AstraBox 是开源、自托管的 Agent 运行平台，将已安装的 Agent 程序变成可以 7×24 小时运行的云端 Agent。企业不必再自建一整套 Agent 基础设施：从 Agent 的构建、部署、运行，到 API 集成、消息渠道触达、身份隔离，全部运行在企业自己控制的部署中。我们缩短了“研发一个 Agent”到“真正上线服务终端用户”的距离，让每一家企业都能快速拥有自己的 AI Agent 产品。

Web 控制台和 HTTP API 覆盖从开发到生产的同一套 Agent 生命周期，两种方式操作相同的资源，并不是两套独立的产品模式。

## Web 控制台

更低门槛地把 Agent 落地到业务场景。由管理员准备好 Agent 与可用资源，用户可以直接开始使用；控制台同时提供消息渠道接入、定时任务、用户认证等业务需要的周边能力。

## HTTP API

为需要完整控制能力的开发者提供底层接口。无需自建 Agent loop、工具执行沙箱，只需通过 API 定义 Agent、启动 Session，并配置 Environment、Skill 和 Session 文件，即可在云端运行复杂任务并实时接收结果。API 同时提供 Environment、Vault、Deployment、Assistant 和消息渠道等管理接口。

## Web 控制台与 HTTP API 对比

| 维度 | Web 控制台 | HTTP API |
| --- | --- | --- |
| 定位 | 在一个界面中提供 Agent 创建、运行、消息渠道、定时任务和认证 | 以 API 开放相同的 Agent 定义和运行资源 |
| 适合谁 | Agent 用户、管理员，以及希望直接使用现成界面的团队 | 希望把 Agent 集成到自己的产品和工作流中的开发者 / 企业 |
| 配置方式 | 管理员通过表单配置 Agent 和资源 | 调用方通过 JSON 请求创建和更新相同的资源 |
| 调用方复杂度 | 较低——由控制台处理请求、事件流和资源导航 | 较高——灵活度高，调用方自行管理 API 请求、事件流和界面 |
| 终端用户身份 | 使用部署中配置的登录方式和 Agent 鉴权 | 使用相同的用户身份或带作用域的客户端凭证 |
| 消息渠道接入 | 创建和管理当前安装的消息渠道 | 通过 API 创建和管理相同的 Deployment 与渠道连接 |
| 定时 / 触发执行 | 创建 Schedule、Webhook 和手动运行 | 通过 API 创建和调用相同的 Deployment |

如果你的目标是直接使用 Agent，推荐从 Web 控制台开始；如果你需要完整控制 Agent 的运行时行为或自建上层产品，请使用 HTTP API。两种方式操作相同的资源，只要调用方拥有访问权限，在一端创建的资源也可以在另一端使用。

## 下一步

- [Web 控制台](quickstart.md)——Agents · Sessions · Deployments · Channels · Environments
- [HTTP API](api.md)——Agents · Sessions · Deployments · Assistants · Environments
