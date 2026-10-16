# API 概览

> 连接已部署的 AstraBox API，并查看它当前提供的准确接口。

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

请将 `astrabox.example.com` 替换为自己的部署地址。发布在子路径下的部署会把该路径放在
`/api/v1` 之前，例如 `https://example.com/astrabox/api/v1`，`/docs` 和 `/openapi.json`
也位于该路径下，见[在子路径下发布 AstraBox](deploy.md#publish-under-a-path)。

## 版本

API 当前为 `v1` 版本。接口使用 `/api/v1` 前缀，无需传递额外的版本头。健康检查、就绪
检查、指标和自动生成的 API 文档使用顶层路径。

## 可用 API 列表

| 资源 | 说明 | 基础路径 |
|---|---|---|
| Agents | Agent 的创建、查询、更新、删除和访问授权 | `/agents` |
| Assistants | Assistant 及其持久工作区管理 | `/assistants` |
| Artifacts | 持久页面、版本、分享和查看器能力管理 | `/artifacts`, `/artifact-share/{token}/runtime` |
| Environments | Agent 可用基础设施管理 | `/admin/environments` |
| Sessions | Session 状态读取、消息发送、事件流和生命周期管理 | `/sessions` |
| Files | Session 工作区文件的查询、上传、移动、下载和删除 | `/sessions/{session_id}/files` |
| Extensions | 为 Agent 分配远程 MCP 服务器和 Skill | `/agents/{agent_id}/extensions` |
| Remote MCP servers | 管理员提供的远程 MCP 连接 | `/admin/mcp-servers` |
| Vaults | Credential 存储，以及 Vault 到 Agent 或 Assistant 的分配 | `/admin/vaults` |
| Deployments | 定时运行 Agent、提供 Webhook 或连接消息产品 | `/admin/agents/{agent_id}/deployments` |
| MCP | 通过 MCP 向客户端提供有权访问的 Agent | `/mcp` |
| Authentication | 浏览器登录、回调、退出和当前登录状态 | `/auth` |
| Administration | 查看沙箱、Session、运行情况和实例配置 | `/admin` |

Session 通过 Agent 或 Assistant 的对话接口创建，不存在独立的 `POST /sessions` 请求体。

## Artifacts

以下路径都相对于 `/api/v1`。JSON 结果使用 `ApiEnvelope`
（`{"code": "OK", "message": "success", "data": ...}`）；实时推送和模型响应使用 SSE。
只有所有者可以使用这些管理接口；访问他人的 Artifact 和不存在的 Artifact 都返回
`404 ARTIFACT_NOT_FOUND`。

| 方法 | 路径 | 输入或结果 |
| --- | --- | --- |
| GET | `/artifacts` | 过滤参数：`cursor`、`limit`、`session_id`、`assistant_id`、`q`；结果：`items`、`next_cursor`、`has_more` |
| GET | `/artifacts/{id}` | Artifact 元数据、`manifest`、来源、内容，以及嵌套的当前 `version` 记录 |
| PATCH | `/artifacts/{id}` | `{title?, description?}` |
| DELETE | `/artifacts/{id}` | 删除 Artifact 及其版本、数据和分享状态 |
| GET | `/artifacts/{id}/versions` | 不含内容的版本元数据 |
| GET | `/artifacts/{id}/versions/{n}` | 版本元数据和内容 |
| POST | `/artifacts/{id}/versions/{n}/restore` | 把保存的内容发布为新版本；返回 Artifact 记录 |
| GET | `/artifacts/{id}/share` | 分享配置 |
| POST | `/artifacts/{id}/share` | `{access, audience, version?, expires_in_seconds?}` |
| DELETE | `/artifacts/{id}/share` | 撤销链接 |

`access` 为 `view` 或 `interact`；`audience` 为 `anyone` 或 `signed_in`。
`version: null` 跟随最新发布；传入数字则固定到该版本。分享状态包含 `enabled`、
`token`、`access`、`audience`、`version`、`created_at` 和 `expires_at`。匿名访问者
只获得 `view`。声明了 `sample` 或 `agent` 的 Artifact 会以
`ARTIFACT_SHARE_AUDIENCE_RESTRICTED` 拒绝 `anyone`；匿名访问其运行时会返回
`401 ARTIFACT_SIGN_IN_REQUIRED`。已撤销、已过期和无效的 token 返回 404。

查看器运行时只有一套接口，通过两个前缀提供：已登录的所有者使用
`/artifacts/{id}/runtime`，通过链接访问的查看者使用 `/artifact-share/{token}/runtime`。
后者在请求带有登录凭证时会接受它；token 不会把匿名查看者提升为 Interact。每次调用
都会检查 Artifact、token、所提供的 manifest 和查看者权限。下表中的 `{runtime}` 可以
替换为任一前缀。匿名调用在每个运行时请求中携带同一个按页面加载生成的 `guest_id`
查询参数。服务端会给这个 id 加上 `guest_` 前缀；它不是用户资料 id。

| 方法 | 路径 | 输入或结果 |
| --- | --- | --- |
| GET | `{runtime}` | `{artifact, version, content, manifest, viewer: {level, id, guest}, pinned, after_seq}`；分享视图还包含 `artifact.share: {version, access, audience}` |
| GET | `{runtime}/live?after_seq=N` | SSE `version`、`data`、`room`、`presence`、`deleted` 和 `error`；日志事件以其序号作为 SSE id |
| POST | `{runtime}/publish` | `{content, base_version}`；仅限所有者，需要 `artifact` 能力，固定版本的链接不可用 |
| GET | `{runtime}/data/{path}` | `{path, exists, data}`；缺失的数据为 `null`；被拒绝的读取返回 `403 ARTIFACT_DATA_DENIED` |
| PUT | `{runtime}/data/{path}` | `{data}` 替换一个文档 |
| PATCH | `{runtime}/data/{path}` | `{data}` 更新一个文档 |
| DELETE | `{runtime}/data/{path}` | 删除一个文档 |
| POST | `{runtime}/data-query` | `{collection, where: [{field, op, value}], order_by: [{field, direction}], limit}` → `{items: [{id, path, data}]}` |
| POST | `{runtime}/data-add` | `{collection, data}` → `{id, path}` |
| POST | `{runtime}/sample` | `{input, max_tokens?, tools?}`；SSE `delta {text}`、`done {text, tool_calls, truncated}`、`error {code, message, text}` |
| POST | `{runtime}/room/{room}/emit` | `{topic, data}` |
| POST | `{runtime}/room/{room}/presence` | `{state}` 或 `{state: null}` |
| GET | `{runtime}/viewers?ids=...` | `{profiles: [{id, display_name}]}`，用于解析每个 Artifact 内不透明的查看者 id |
| POST | `{runtime}/agent-runs` | `{prompt}` → `{run_id, session_id, url}` |
| GET | `{runtime}/agent-runs/{run_id}` | `{state, session_id, error?}` |

创建 Artifact 和编辑内容使用内置的 `artifacts` MCP 工具：`create_artifact`、
`update_artifact`、`get_artifact`、`list_artifacts`、`artifact_data` 和
`artifact_guide`。所有者没有用于创建的 REST 接口。内容更新、页面发布和恢复都会创建
不可变的版本。`update_artifact` 接受 `content`、`file_path` 或
`edits: [{old_string, new_string, replace_all?}]`，并可选传入 `base_version`。基于
过期版本的编辑返回 `ARTIFACT_VERSION_CONFLICT`。标识某个 Artifact 的工具结果带有
`astrabox_artifact: {id, version, title, kind}`。

Artifact 日志与对话事件流相互独立，不会给对话事件流引入新的事件类型。能力声明、
授权确认、隔离和大小限制见 [Artifact 手册](artifacts.md)。

## 请求大小限制

AstraBox 不为所有路由设置同一个 JSON 请求体上限。反向代理可以设置部署级上限，各接口
也会按照资源需要执行自己的限制。文件上传和下载都使用流式传输：上传先暂存到服务端的
临时文件，再流式写入沙箱；下载在客户端读取时从沙箱流式传出。AstraBox 对两者都不设
大小上限。上传需要服务端有容纳该文件的磁盘空间；沙箱把文件写入目标位置期间，沙箱内还需要
两倍于文件大小的空间。

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
2. 在生产环境中固定所使用的 AstraBox 版本
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
