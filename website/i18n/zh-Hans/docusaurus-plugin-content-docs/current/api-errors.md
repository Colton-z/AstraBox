# 错误参考

AstraBox API 使用统一的错误信封格式返回错误。每个错误响应包含结构化信息，便于程序化
处理和问题排查。

## 错误信封格式

AstraBox REST 资源接口使用以下 JSON 结构：

```json
{
  "code": "SESSION_BUSY",
  "message": "session is busy",
  "data": null,
  "error": {
    "code": "SESSION_BUSY",
    "status_code": 409,
    "category": "state",
    "retryable": true,
    "owner": "session",
    "user_message": "session is busy"
  }
}
```

每个响应还会携带 W3C `traceparent` 响应头。意外的服务端错误还会把相同的 Trace ID
放在 `data.trace_id` 中，方便管理员关联响应与日志。

### 字段说明

| 字段 | 类型 | 必有 | 说明 |
|---|---|---|---|
| `code` | string | 是 | 用于程序化处理的稳定错误码 |
| `message` | string | 是 | 可以向调用方显示的消息 |
| `data` | any \| null | 是 | 错误提供的结构化数据 |
| `error.code` | string | 是 | 与顶层 `code` 相同的稳定错误码 |
| `error.status_code` | integer | 是 | 为错误码登记的状态；传输处理应使用实际 HTTP 响应状态码 |
| `error.category` | string | 是 | 错误类别，例如 `request`、`auth`、`state` 或 `persistence` |
| `error.retryable` | boolean | 是 | 不修改请求、等待一段时间后重试是否可能成功 |
| `error.owner` | string | 是 | 需要采取行动的组件或一方：`client`、`session`、`mongo`、`runtime`、`template`、`platform` 或 `unknown` |
| `error.user_message` | string | 是 | 可以向调用方显示的消息 |
| `error.debug_message` | string | 否 | 错误提供的补充诊断消息 |
| `error.evidence` | object | 否 | 用于诊断的结构化证据 |
| `error.cause_code` | string | 否 | 存在时为下层原因错误码 |

## 错误类型一览

错误码描述具体故障，HTTP 状态码描述请求如何结束。常见状态类别如下：

| HTTP 状态码 | `code` 示例 | 说明 |
|---|---|---|
| 400 或 422 | `INVALID_REQUEST` | 请求参数无效或缺失 |
| 401 | `AUTH_REQUIRED`、`UNAUTHORIZED`、`TOKEN_EXPIRED` | 认证失败或需要认证 |
| 403 | `FORBIDDEN`、`API_TOKEN_SCOPE_INSUFFICIENT` | 认证成功但没有执行操作的权限 |
| 404 | `NOT_FOUND`、`SESSION_NOT_FOUND` | 目标资源不存在或不可访问 |
| 409 | `SESSION_BUSY`、`IDEMPOTENCY_KEY_CONFLICT` | 资源状态与当前操作冲突 |
| 429 | `ADMISSION_DENIED` | 部署的准入策略拒绝任务 |
| 499 | `REQUEST_CANCELLED` | 调用方取消了请求 |
| 5xx | `PERSISTENCE_UNAVAILABLE`、`UNEXPECTED_SERVER_ERROR` | AstraBox、基础设施或上游依赖发生故障 |

表中仅列出示例，并非完整的错误码目录。请查看部署版本的 `/openapi.json`，并读取失败
接口实际返回的字段。

## 各错误类型详解

### 400 或 422 — `INVALID_REQUEST`

请求格式或参数不合法。

**常见触发场景：**

- 缺少必需字段（如 `permission_mode`）
- 字段值类型错误（如 `string` 传了 `number`）
- 参数超出允许范围
- JSON 格式错误

```json
{
  "code": "INVALID_REQUEST",
  "message": "permission_mode is required",
  "data": null,
  "error": {
    "code": "INVALID_REQUEST",
    "status_code": 400,
    "category": "request",
    "retryable": false,
    "owner": "client",
    "user_message": "permission_mode is required"
  }
}
```

```bash
# 触发示例：缺少 permission_mode 字段
curl --fail-with-body --silent --show-error \
  -X POST "$SERVICE_URL/api/v1/sessions/$SESSION_ID/permission-mode" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### 401 — 认证错误

身份认证失败。

**常见触发场景：**

- 受保护接口未携带凭证
- Bearer Token 格式错误或无效
- Access Token 已过期或被撤销
- 凭证来自不受信任的签发方

```json
{
  "code": "TOKEN_EXPIRED",
  "message": "bearer token expired; obtain a new one from your token issuer",
  "data": null,
  "error": {
    "code": "TOKEN_EXPIRED",
    "status_code": 401,
    "category": "auth",
    "retryable": false,
    "owner": "client",
    "user_message": "bearer token expired; obtain a new one from your token issuer"
  }
}
```

```bash
# 触发示例：使用无效 Token
curl --fail-with-body --silent --show-error \
  "$SERVICE_URL/api/v1/agents" \
  -H "Authorization: Bearer invalid-token"
```

### 403 — 鉴权错误

调用方已通过认证，但没有执行操作的权限。

**常见触发场景：**

- 当前身份不能管理目标 Agent
- Access Token 不包含当前操作所需的权限
- 非管理员调用管理接口

```json
{
  "code": "API_TOKEN_SCOPE_INSUFFICIENT",
  "message": "API token requires scope astrabox:admin",
  "data": { "required_scope": "astrabox:admin" },
  "error": {
    "code": "API_TOKEN_SCOPE_INSUFFICIENT",
    "status_code": 403,
    "category": "auth",
    "retryable": false,
    "owner": "client",
    "user_message": "API token requires scope astrabox:admin"
  }
}
```

```bash
# 触发示例：使用只读 Token 调用管理 API
curl --fail-with-body --silent --show-error \
  "$SERVICE_URL/api/v1/admin/environments" \
  -H "Authorization: Bearer $READ_TOKEN"
```

### 404 — 未找到错误

目标资源不存在，或调用方无法查看该资源。

**常见触发场景：**

- Agent、Session 或 Environment ID 不存在
- 资源已被删除
- 调用方无权发现其他所有者的资源
- URL 路径拼写错误

```json
{
  "code": "SESSION_NOT_FOUND",
  "message": "session not found",
  "data": null,
  "error": {
    "code": "SESSION_NOT_FOUND",
    "status_code": 404,
    "category": "request",
    "retryable": false,
    "owner": "session",
    "user_message": "session not found"
  }
}
```

```bash
# 触发示例：查询不存在的 Session
curl --fail-with-body --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/session_nonexistent_123" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

### 409 — 冲突错误

资源状态冲突，操作无法执行。

**常见触发场景：**

- 使用相同幂等键创建不同的对话
- Session 正在处理另一个 Turn
- Agent 或 Environment 状态不允许当前操作

```json
{
  "code": "SESSION_BUSY",
  "message": "session is busy",
  "data": null,
  "error": {
    "code": "SESSION_BUSY",
    "status_code": 409,
    "category": "state",
    "retryable": true,
    "owner": "session",
    "user_message": "session is busy"
  }
}
```

### 5xx — 服务端和依赖错误

AstraBox、基础设施或上游依赖发生故障。

**常见触发场景：**

- 数据库不可用
- 沙箱或 Agent 程序启动失败
- 身份提供商或模型网关不可用
- 意外的内部故障

```json
{
  "code": "PERSISTENCE_UNAVAILABLE",
  "message": "mongodb timeout/unavailable, please retry",
  "data": null,
  "error": {
    "code": "PERSISTENCE_UNAVAILABLE",
    "status_code": 503,
    "category": "persistence",
    "retryable": true,
    "owner": "mongo",
    "user_message": "mongodb timeout/unavailable, please retry"
  }
}
```

<Note>`error.retryable` 为 `true`，或者 `429` 响应提供 `data.retry_after_seconds` 时
可以重试。请使用有上限的指数退避，并至少等待响应指定的秒数。</Note>

## 错误处理最佳实践

1. 根据 `code` 和 `error.retryable` 进行程序化判断，而非只看 HTTP 状态码
2. 记录 `traceparent` 响应头、`code` 和 `message`，用于日志排查
3. 如果存在，检查 `data`、`error.evidence` 和 `error.cause_code`
4. `error.retryable` 为 `false` 时不要重试，除非 `429` 响应提供 `data.retry_after_seconds`
5. 对可重试的响应使用有上限的指数退避

```bash
# 带错误处理的请求示例
headers=$(mktemp)
trap 'rm -f "$headers"' EXIT
response=$(curl --silent --show-error -D "$headers" -w "\n%{http_code}" \
  "$SERVICE_URL/api/v1/agents" \
  -H "Authorization: Bearer $ACCESS_TOKEN")

# 提取 HTTP 状态码
http_code=$(echo "$response" | tail -1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" -ge 400 ]; then
  # 提取错误码
  error_code=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['code'])")
  retryable=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['error']['retryable'])")
  traceparent=$(sed -n 's/^[Tt]raceparent: //p' "$headers" | tr -d '\r')
  echo "API 错误: $error_code retryable=$retryable traceparent=$traceparent"
fi
```

## 下一步

- [概览](overview.md) — 了解 AstraBox 的整体架构。
