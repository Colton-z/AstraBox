# 概览

AstraBox 是开源、自托管的 Agent 运行平台，可将已安装的 Agent 程序变成随时可用的云端 Agent。你无需自建 agent loop、管理工具执行沙箱或处理长连接——只需在管理台或通过 API 定义 Agent、启动 Session，即可在云端运行复杂任务并实时接收结果。

## 核心概念

| 概念              | 说明                         | 类比          |
| --------------- | -------------------------- | ----------- |
| **Agent**       | 由已安装 Agent 程序驱动的云端 Agent       | "云端同事"      |
| **Environment** | Session 的运行环境，包含 Agent 程序、沙箱、模型连接和网络配置 | "办公桌和工具箱" |
| **Session**     | Agent 的一次有状态运行，包含消息、Event 和当前状态 | "一项具体工作" |
| **Event**       | Session 中产生的实时事件流          | "工作进度实时播报"  |

## 工作流程

1. **定义 Agent。** 指定模型、系统提示词（system prompt）和扩展。
2. **配置 Environment。** 选择 Agent 程序、沙箱、模型连接和网络配置。
3. **启动 Session。** 使用 Agent 创建运行实例。
4. **发消息 + 收事件。** 向 Session 发送消息，然后通过 HTTP 实时接收 Agent 消息、执行进度和状态变更。

## 开箱即用的企业级基建

一次部署就带齐团队通常要自己拼装的几块：

- **模型网关。** 默认内置 [LiteLLM](https://github.com/BerriAI/litellm) 网关。Agent 只选路由名；上游密钥、路由、预算和请求日志都留在服务端，后面可以接 Anthropic、OpenAI 兼容服务或本地模型。参见[连接模型服务](models.md)。
- **团队登录。** 预集成 [Casdoor](https://github.com/casdoor/casdoor) 作为身份提供方，支持 OIDC、组织与角色，可用钉钉、企业微信、飞书、GitHub 等账号登录。加一个 Compose 覆盖文件即可开启，参见[设置团队登录](team-login.md)。
- **隔离沙箱。** 基于 [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox)，单台 Docker 主机或 Kubernetes 集群都能运行，并预留热容量，对话秒级拉起、秒级恢复。
- **凭证不进沙箱。** Vault 凭证在沙箱的出站边界注入，Agent 只看得到占位符。参见[保护 Agent 使用的凭证](egress-credential-injection.md)。
- **触发器与消息通道。** 定时任务、签名 Webhook 和消息平台可以在无人值守时启动 Agent。参见[让 Agent 自动运行](deployments.md)。

## 快速验证连通性

```bash
# 验证本地 API，列出所有 Agent
curl -s http://127.0.0.1:8088/api/v1/agents
```

成功响应格式：

```json
{
  "code": "OK",
  "message": "success",
  "data": []
}
```

## 适用场景

- **长时间异步任务** — 代码审查、大规模重构、自动化测试生成
- **API 集成** — 在后端服务中嵌入 Agent 能力，无需另外维护运行时
- **批量处理** — 并行启动多个 Session 处理批量请求
- **定时任务** — 创建定时 Deployment，周期性运行 Agent 完成巡检或报告

## 认证方式

团队部署使用 OAuth 访问令牌时，API 请求需要携带以下 Header：

| Header          | 值                         | 说明                 |
| --------------- | -------------------------- | -------------------- |
| `Authorization` | `Bearer <access token>`    | 由已配置身份提供商签发的访问令牌 |

:::note
本地模式不启用 API 认证，只适合通过回环地址使用。团队部署使用已配置的身份提供方，例如 OIDC、验证 JWT、可信网关请求头或已安装的身份提供方。详见[团队认证](team-login.md)。
:::

## 分页机制

不同资源的列表接口采用各自的分页结构。[分页](api-pagination.md)列出了支持的游标和页码形式。

## 常见问题

**Q: AstraBox Agent 和本地的 Agent 程序可以同时使用吗？**

A: 可以。本地使用适合在一台电脑上交互开发；AstraBox Agent 则可以远程访问、执行长时间任务并接入其他系统，两者互补。

**Q: 一个 Agent 可以同时运行多少个 Session？**

A: 同一个 Agent 可以同时运行多个活跃 Session，实际数量取决于部署的计算资源和沙箱容量。

**Q: 数据安全如何保障？**

A: 默认情况下，每个 Session 运行在隔离沙箱中；Environment 也可以使用平台支持的 Agent 共享沙箱。Session 记录保存在沙箱之外；工作区生命周期取决于 Environment 的空闲处理方式和存储配置。

## 下一步

- [快速开始](quickstart.md) — 运行第一个 Agent
- [定义 Agent](authoring-agents.md) — 深入了解 Agent 配置
- [Environment](environments.md) — 配置运行环境
- [启动 Session](sessions.md) — 管理会话生命周期
