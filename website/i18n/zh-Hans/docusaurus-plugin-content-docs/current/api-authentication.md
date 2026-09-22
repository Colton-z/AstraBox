# 认证

> 使用浏览器登录 Cookie 或 OAuth Access Token 认证 AstraBox API 请求。

使用 OIDC 的 AstraBox 部署支持两种凭证：<strong>浏览器登录 Cookie</strong>和
<strong>OAuth Access Token</strong>。受保护的 API 请求必须携带其中一种有效凭证。

| 凭证 | 适用场景 | 获取方式 |
|---|---|---|
| 浏览器登录 Cookie | 用户身份调用，适合浏览器应用和 AstraBox 控制台 | 通过配置的身份提供商登录 |
| OAuth Access Token | 机器身份调用，适合服务端集成和自动化 | 使用 OAuth 机密客户端的凭证置换短期 Token |

<Note>OAuth Client Secret 是用于获取 Access Token 的长期凭证，不应直接用于调用
AstraBox API。</Note>

## 设置服务地址

API 请求使用自托管 AstraBox 部署的公网地址。请设置服务地址：

```bash
export SERVICE_URL="https://astrabox.example.com"
```

本地安装使用 `http://127.0.0.1:8088`。

## 方式一：使用浏览器登录 Cookie

### 通过身份提供商登录

1. 打开 AstraBox 控制台
2. 点击**登录**
3. 通过配置的身份提供商完成认证
4. 从同一浏览器地址调用 API；浏览器会自动发送签名的 HTTP-only Cookie

<Note>JavaScript 无法读取浏览器 Cookie。浏览器应用应发送同源请求，不要把 Cookie
复制到认证请求头中。</Note>

## 方式二：使用 OAuth Client 和 Access Token

### 获取并设置 Client Credential

1. 在身份提供商中创建 OAuth 机密客户端
2. 允许 `client_credentials` grant
3. 根据调用范围授予所需权限：
   - 读取非管理 API：`astrabox:read`
   - 写入非管理 API：`astrabox:write`
   - 管理 API：`astrabox:admin`
4. 复制 Client ID 和 Client Secret，并设置环境变量：

```bash
export CLIENT_ID="astrabox-api"
export CLIENT_SECRET="client-secret"
```

<Note>Client Secret 是长期凭证。请将它保存在密钥管理系统中，不要写入代码或日志。</Note>

### 置换 Access Token

调用身份提供商的 Token Endpoint，使用 Client Credential 置换 Access Token。使用
AstraBox 随附的本地身份提供商时：

```bash
ACCESS_TOKEN="$(
  curl --fail --silent --show-error \
    --user "$CLIENT_ID:$CLIENT_SECRET" \
    --data-urlencode grant_type=client_credentials \
    --data-urlencode 'scope=astrabox:read' \
    http://127.0.0.1:8087/api/login/oauth/access_token \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'
)"
export ACCESS_TOKEN
```

| 字段 | 说明 |
|---|---|
| `grant_type` | 固定为 `client_credentials` |
| `scope` | 以空格分隔的 `astrabox:read`、`astrabox:write` 和 `astrabox:admin` 子集，不能超出 OAuth Client 被授予的权限 |

响应中的 `access_token` 即 Access Token。Access Token 过期后，需要使用 Client
Credential 重新获取。

### 使用一个 Access Token 获取所需的 API 权限

如果服务端集成需要使用同一个 Access Token 读取 Session 并管理实例配置，请同时申请
`astrabox:read` 和 `astrabox:admin`：

```bash
ACCESS_TOKEN="$(
  curl --fail --silent --show-error \
    --user "$CLIENT_ID:$CLIENT_SECRET" \
    --data-urlencode grant_type=client_credentials \
    --data-urlencode 'scope=astrabox:read astrabox:admin' \
    http://127.0.0.1:8087/api/login/oauth/access_token \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'
)"
export ACCESS_TOKEN
```

<Warning>
  包含 `astrabox:admin` 的 Access Token 可以管理实例级配置。仅应在受信任的服务端
  使用；请勿提供给终端用户或不受信任的客户端。请为每个环境使用单独的 OAuth Client，
  并且只授予每项集成所需的权限。
</Warning>

同一个 Token 可以调用其权限允许的所有接口：

```bash
# 读取 API
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/sessions" \
  -H "Authorization: Bearer $ACCESS_TOKEN"

# 管理 API
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/admin/environments" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

### 兼容其他认证方式

- JWT 方式接受由配置的签发方、受众、算法和签名密钥验证的 Bearer JWT
- Trusted Header 方式从认证网关接收身份；API 客户端向该网关完成认证
- Local 方式无需凭证，仅适合单人使用且仅监听回环地址的安装

部署侧配置见[认证](team-login.md)。

## Bearer 头格式

使用 OAuth Access Token 或已验证的 JWT 调用 AstraBox API 时，通过 Bearer 方式传递：

```text
Authorization: Bearer <access-token>
```

建议将当前选用的 Token 设置为统一环境变量：

```bash
export ACCESS_TOKEN="access-token"
```

完整请求示例：

```bash
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/agents" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

## 安全建议

- 为开发、测试和生产环境使用单独的 OAuth Client 或 JWT 签发方
- 将 Client Secret 和签名密钥存储在密钥管理系统中，不要硬编码
- 只为每个 Client 授予集成所需的权限
- 在当前 Access Token 过期前获取替代 Token，并安全替换运行中使用的 Token
- 发现 Client Secret、Token 或签名密钥泄漏时，立即在身份提供商中撤销或轮换
