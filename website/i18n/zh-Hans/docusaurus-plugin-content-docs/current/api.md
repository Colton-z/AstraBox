# API 概览

AstraBox API 提供完整的自托管云端 Agent 管理能力，涵盖 Agent 创建、Environment 配置、
Session 生命周期、事件流、文件、Deployment 和凭证等功能。REST 接口使用 JSON 作为
请求和响应格式，流式接口使用 Server-Sent Events。

<Note>每个 AstraBox 实例都会在 `/docs` 提供交互式 API 文档；`/openapi.json` 中的
OpenAPI 文档描述当前部署版本的准确 API 接口。</Note>

## Gateway URL

| 环境 | URL |
|---|---|
| 自托管生产环境 | `https://astrabox.example.com/api/v1` |
| 本地开发 | `http://127.0.0.1:8088/api/v1` |

请将 `astrabox.example.com` 替换为自己的部署地址。

## 版本

API 当前为 `v1` 版本。接口使用 `/api/v1` 前缀，无需传递额外的版本头。健康检查、就绪
检查、指标和自动生成的 API 文档使用顶层路径。

## 可用 API 列表

| 资源 | 说明 | 基础路径 |
|---|---|---|
| Agents | Agent 的创建、查询、更新、删除和鉴权 | `/agents` |
| Assistants | Assistant 及其持久工作区管理 | `/assistants` |
| Environments | Agent 可用基础设施管理 | `/admin/environments` |
| Sessions | Session 状态、Turn 提交、事件流和生命周期管理 | `/sessions` |
| Files | Session 工作区文件的查询、上传、移动、下载和删除 | `/sessions/{session_id}/files` |
| Extensions | 为 Agent 分配远程 MCP 服务器和 Skill | `/agents/{agent_id}/extensions` |
| Remote MCP servers | 管理员提供的远程 MCP 连接 | `/admin/mcp-servers` |
| Vaults | Credential 存储，以及 Vault 到 Agent 或 Assistant 的分配 | `/admin/vaults` |
| Deployments | 定时运行 Agent、提供 Webhook 或连接消息产品 | `/admin/agents/{agent_id}/deployments` |
| MCP | 通过 MCP 向客户端提供有权访问的 Agent | `/mcp` |
| Authentication | 浏览器登录、回调、退出和当前登录状态 | `/auth` |
| Administration | 沙箱、Session、系统运行和实例配置管理 | `/admin` |

Session 通过 Agent 或 Assistant 的对话接口创建，不存在独立的 `POST /sessions` 请求体。

## 请求大小限制

AstraBox 不为所有路由设置同一个 JSON 请求体上限。反向代理可以设置部署级上限，各接口
也会按照资源需要执行自己的限制。文件上传使用流式传输；Files API 单次下载限制为
64 MiB，避免一个响应占用无限的 API 进程内存。

通过代理运行时，请根据部署允许的最大操作配置请求、响应和流式传输超时。请求过大时，
应由代理返回 `413`。

## 必需请求头

团队部署的受保护路由需要有效的浏览器 Cookie 或 Bearer Token。JSON 请求应携带
`Content-Type`：

```text
Authorization: Bearer $ACCESS_TOKEN
Content-Type: application/json
```

仅在回环地址运行的本地身份模式无需 `Authorization`。创建对话时还可以传入
`Idempotency-Key`；同一个 Key 只能用于同一用户与同一 Agent 或 Assistant。

## 版本兼容性

1. API 接口属于当前安装的 AstraBox 版本
2. 在生产环境中固定使用的 AstraBox 版本
3. 升级时检查该版本的 `/openapi.json` 文档，并重新生成强类型客户端

## 快速验证连通性

```bash
# 列出当前身份可以访问的 Agent
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/agents" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

成功响应示例：

```json
{
  "code": "OK",
  "message": "success",
  "data": []
}
```

## 限流说明

API 应用层默认不主动限流。管理员可以安装准入策略，也可以在反向代理或网关设置流量
限制。这些规则可能返回 `429`；基础设施不可用时可能返回 `503`。

客户端应限制并发，并对 `429` 和可重试的 `5xx` 响应使用有上限的指数退避。如果准入
拒绝中包含 `data.retry_after_seconds`，应至少等待相应时间后再重试。

## 下一步

- [认证](api-authentication.md) — 对 API 请求进行认证。
- [错误参考](api-errors.md) — 错误码与排查。
- [分页](api-pagination.md) — 列表接口分页。
