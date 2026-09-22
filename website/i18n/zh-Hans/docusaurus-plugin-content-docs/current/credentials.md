# 用户凭证

Agent 经常需要访问第三方服务——GitHub、Jira、数据库、自建的远程 MCP 服务器等。Vaults 提供安全的凭证托管，让你把 Token 保存在自己的 AstraBox 部署中，Session 运行时按需用于服务请求，无需硬编码在代码里。

## 核心概念

| 概念 | 说明 |
|---|---|
| Vault | 凭证容器，可包含多个 Credential |
| Credential | 单条凭证记录，绑定到服务 URL 或环境变量名 |
| `auth.type` | 凭证认证方式：MCP 服务 bearer token（`static_bearer`）、MCP 服务 OAuth token（`mcp_oauth`）、MCP 服务 API-key 请求头（`mcp_static_header`）、HTTPS 目标的 HTTP Basic（`http_basic`）或其他服务的环境变量（`environment_variable`） |
| `vault_ids` | 分配给 Agent 或 Assistant 的有序 Vault ID 列表 |

## 安全性

- `access_token`**永远不会**在 API 响应中返回
- `token`、`password`、`refresh_token`、`client_secret` 等其他密文也永远不会返回
- 凭证在服务端加密存储
- 凭证只提供给已分配的工作负载。Agent 预热也会使用已分配的 HTTP Basic 凭证，
  在 Session 创建前下载私有 Skill 和 Plugin。

## 完整流程

### 1. 创建 Vault

在**管理台 → 凭证**中创建 Vault，或使用管理 API：

```bash
curl -X POST https://astrabox.example.com/api/v1/admin/vaults \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "我的 GitHub 凭证",
    "metadata": {}
  }'
```

响应示例：

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "vault_id": "vlt_8a15f9c8d1d34cf4b7b485d735c77d75",
    "display_name": "我的 GitHub 凭证",
    "metadata": {},
    "archived_at": null,
    "created_at": "2026-08-24T08:00:00Z",
    "updated_at": "2026-08-24T08:00:00Z"
  }
}
```

### 2. 添加 Credential {#2-add-a-credential}

使用 static Bearer token 时，通过 nested `auth` 为 Vault 添加 Credential：

一个目标可以接收多个 Vault。Session 创建时使用当时的分配。移除分配后，之后创建的
Session 将不会再接收该 Vault。平台会自动允许活动出站凭证 binding 指定的精确目标地址；
这些地址已经属于沙箱的有效策略，Environment 允许列表无需重复填写。

```bash
curl -X POST \
  https://astrabox.example.com/api/v1/admin/vaults/vlt_8a15f9c8d1d34cf4b7b485d735c77d75/credentials \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "auth": {
      "type": "static_bearer",
      "mcp_server_url": "https://jira.example.com/mcp",
      "token": "jira_token_xxxxxxxx"
    }
  }'
```

响应返回 `credential_id` 和脱敏后的 `auth` 对象，不包含密文。

使用 MCP OAuth 时，导入 access token 和可选的 refresh 配置：

```bash
curl -X POST \
  https://astrabox.example.com/api/v1/admin/vaults/vlt_8a15f9c8d1d34cf4b7b485d735c77d75/credentials \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "auth": {
      "type": "mcp_oauth",
      "mcp_server_url": "https://mcp.linear.app/mcp",
      "access_token": "access_token_xxxxxxxx",
      "expires_at": "2026-08-24T09:00:00Z",
      "refresh": {
        "token_endpoint": "https://api.linear.app/oauth/token",
        "client_id": "astrabox",
        "auth_method": "none",
        "refresh_token": "refresh_token_xxxxxxxx"
      }
    }
  }'
```

AstraBox 不负责浏览器授权流程。请先从远程 MCP 服务获得 Token，再将其保存到 Vault。
Credential 包含有效的 refresh 配置时，AstraBox 会在准备或重连运行时、以及每项顶层
Agent 任务开始前刷新已经过期的 access token。它不会在每次 MCP HTTP 请求前检查有效期。

使用自定义请求头认证的服务选择 `mcp_static_header`。需要把其他服务凭证作为环境变量
提供时选择 `environment_variable`；主机和请求范围限制见
[保护 Agent 使用的凭证](egress-credential-injection.md)。

通过 HTTPS 访问私有 Git 仓库时，选择 `http_basic`，配置 `auth.url`、`auth.username`
和只写的 `auth.password`。URL 必须使用 HTTPS 443 端口和非根路径，不含嵌入凭证、
查询参数、片段或通配符。用户名不能包含冒号或控制字符；密码可以是 Git 服务要求的
仓库访问 Token。请按该服务要求选择用户名和仓库权限。

### 3. 在 Session 中使用

在**管理台 → 凭证**中把 Vault 分配给 Agent 或 Assistant，也可以通过管理 API
设置有序的 `vault_ids`：

```bash
curl -X PUT \
  https://astrabox.example.com/api/v1/admin/agents/agent_xxx/credential-vaults \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "vault_ids": ["vlt_8a15f9c8d1d34cf4b7b485d735c77d75"]
  }'
```

此后从该 Agent 创建的新 Session 会自动获得 Vault 中所有有效 Credential 的访问权限。
使用 MCP 凭证时，按照分配顺序找到的第一个相同 MCP 服务器 URL 生效。HTTP Basic
凭证按保存的完整目标 URL 匹配，同一 URL 使用分配顺序中的第一个 Vault。Assistant
可以使用 MCP 凭证，但不能分配包含 `environment_variable` 或 `http_basic` 凭证的 Vault。

## 参数说明

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `display_name` | string | 是 | Vault 显示名称 |
| `metadata` | object | 否 | 自定义元数据 |
| `auth.type` | string | 创建凭证时是 | `static_bearer`、`mcp_oauth`、`mcp_static_header`、`http_basic` 或 `environment_variable` |
| `auth.url` | string | `http_basic` 必填 | 不含凭证、路径非根路径的 HTTPS 目标 URL |
| `auth.username` | string | `http_basic` 必填 | HTTP Basic 用户名；不含冒号或控制字符 |
| `auth.password` | string | `http_basic` 必填 | 密码或 Token，只写 |
| `auth.mcp_server_url` | string | MCP 凭证必填 | 远程 MCP 服务器地址 |
| `auth.token` | string | `static_bearer` 必填 | Bearer Token 值，只写 |
| `auth.access_token` | string | 导入 `mcp_oauth` 时必填 | OAuth access token，只写 |
| `auth.header_name` | string | `mcp_static_header` 必填 | 自定义请求头名称 |
| `auth.value` | string | `mcp_static_header` 必填 | 自定义请求头值，只写 |
| `auth.secret_name` | string | `environment_variable` 必填 | 环境变量名 |
| `auth.secret_value` | string | `environment_variable` 必填 | 密钥值，只写 |
| `auth.expires_at` | string | 否 | OAuth access token 过期时间，RFC 3339 格式 |
| `auth.refresh` | object | 否 | OAuth refresh 配置 |

## 常见问题

**Q: MCP OAuth token 过期后怎么办？** A: Credential 包含 refresh token 和 refresh
配置时，AstraBox 会在准备或重连运行时、以及下一项顶层 Agent 任务开始前刷新过期 Token。
它不会在每次 MCP HTTP 请求前检查有效期。没有 refresh 配置或刷新已经失效时，
请轮换 Credential。

**Q: 能否更新已有 Credential 的 Token？** A: 可以。`PATCH` 请求会轮换本次提交的
只写密钥字段。凭证类型、MCP 服务器 URL、自定义请求头名称、HTTP Basic 目标 URL 和
用户名、环境变量名、OAuth token endpoint 和 OAuth client ID 不可修改；需要改变这些
字段时，请归档原 Credential 并创建新的 Credential。

**Q: 一个 Session 可以关联多少个 Vault？** A: 没有硬性限制，但建议按服务分组管理，保持清晰。

**Q: Token 泄露了怎么办？** A: 立即删除对应 Credential 并在第三方平台吊销 Token，然后创建新的 Credential。

**Q: 我能查看已存储的 Token 吗？** A: 不能。出于安全考虑，credential 密文只写，
只能轮换、归档或删除。

> 建议为不同环境（开发/生产）创建独立的 Vault，避免混用凭证。
