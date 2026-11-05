# 外部聊天接入 Satori

AstraBox 在 `/api/satori/agents/{agent_id}/v1` 提供固定 Agent 的 Satori v1
服务端入口。业务后端直接连接这里，无需另部署 Satori 服务端。现有渠道配置中的
**Satori Protocol Server** 则是 AstraBox 连接外部服务端的客户端，两者方向不同。

员工继续登录业务系统，无需进入 AstraBox 登录页。部署方的 `WebIdentityResolver`
验证业务后端代表哪位员工执行，并返回现有 `UserContext`，用于个人资源授权、Agent
权限及会话隔离。可以验证委托令牌，也可以验证可信后端凭据与员工声明的绑定；绑定
由 provider 检查。平台不会把普通工号字段直接当作执行身份。固定后端凭据本身不能
读取其他员工的会话。内置无鉴权模式仍是单用户模式，不适合向不可信调用者开放。
Satori 提供令牌传递方式，不提供企业员工目录或个人同意授权服务。

## 映射和鉴权

| Satori 对象 | 平台含义 |
| --- | --- |
| Login | Agent 机器人：`platform=astrabox`，`user.id=agent_id` |
| Guild | id 为 `agent_id` 的虚拟会话命名空间，不是真实社交群组 |
| Channel | 当前执行人拥有的核心 Session；每次创建都是独立对话 |
| User | 已验证的员工执行身份，或输出消息的 Agent 机器人 |
| `message.create` 返回的 Message | 已接受的 Input，以 `client_message_id` 为 id |
| 助手 Message | 以核心 `response_id` 为 id，更新时包含累积文本 |

HTTP 携带 `Authorization: Bearer <provider 可验证的凭据>`、
`Satori-Platform: astrabox`、`Satori-User-ID: <agent_id>`。最后一个请求头选择
Agent 登录号，**不是员工身份**。可信声明请求头由部署方 provider 解释和验证。
provider 返回 API scopes 时，读取要求 `astrabox:read`，写入要求 `astrabox:write`。
每次操作均检查 Agent 使用权限与 Session 所有权。返回标准 Satori 对象，不套控制台
响应信封；错误以 HTTP 状态及 `{code, message, data}` 表达。

## 标准 HTTP API

均使用 POST 和 JSON 对象，`upload.create` 例外，使用 `multipart/form-data`；不支持的 API 返回 404。

| `/v1/` 后的路径 | 参数 | 结果 |
| --- | --- | --- |
| `login.get` | `{}` | Login 与支持的标准 API 列表 |
| `channel.create` | `{guild_id: agent_id, data: {type: 0, name?: string}}` | 新 Channel |
| `channel.list` | `{guild_id: agent_id, next?: string}` | 自己的 Channel，最多 100 个及下一页令牌 |
| `channel.get` | `{channel_id}` | 自己的 Channel |
| `message.create` | `{channel_id, content}` | 含已接受 Input 的数组 |
| `upload.create` | multipart：每个文件一个 part | `{part 名: 资源链接}` |
| `message.get` | `{channel_id, message_id}` | 消息文本投影 |
| `message.list` | `{channel_id, next?, direction?: "before", order?: "asc" 或 "desc", limit?: number}` | 原生顺序历史页及 `prev`／`next` |

历史从最新一页开始向前翻页，默认页内按原生顺序升序，默认 50 条，上限 100 条。
目前只支持 `before` 方向，其余方向明确拒绝。文本按 Satori 格式转义。标准 Message
的 content 是消息文本，后面跟着它携带或写出的每个文件对应的 `<img>`／`<file>` 元素
（见[附件](#附件)）。完整的推理、工具及交互通过下面的原生扩展读取。

重试创建会话或发送同一业务消息时，传稳定的 `Idempotency-Key`，长度 1–128。
分别复用核心会话创建键和 Session 内的 Input 去重键；重复消息键返回原 Input，
不再次执行。不同消息使用不同键。发送成功仅表示**已接受**，不是模型已执行完成。

## 授权与交互扩展

下列是明确声明的 Satori internal API，路径为 `/v1/internal/<方法>`，不是标准
资源中的自定义字段。

| 方法 | 参数 | 结果 |
| --- | --- | --- |
| `astrabox.preparation.get` | `{}` | `{applicable, ready, user_action, actions}` |
| `astrabox.preparation.perform` | `{action_id}` | 更新后的准备状态 |
| `astrabox.history` | `{channel_id, before?}` | 完整公开 UI 消息、原生顺序、分页及当前交互 |
| `astrabox.conversation.get` | `{channel_id}` | Session、当前／最近响应、待执行输入及交互 |
| `astrabox.interaction.answer` | `{channel_id, interaction_id, answer}` | 核心交互回答回执 |
| `astrabox.response.stop` | `{channel_id}` | 核心停止请求回执 |
| `astrabox.files.list` | `{channel_id, path?}` | Channel 工作区目录，每个文件带资源 `url` |

先查询准备状态，再创建 Channel。查询不发起同意、不签发凭据、不创建 Session 或
沙箱。前端展示 provider 返回的用户操作入口，仅执行当前提供的 action。平台不替
用户授权。此时 provider 收到 `SessionUserActionContext(session_id=None,
principal_id=员工执行身份, subject=PreparationSubject("agent", agent_id))`。
个人委托应以员工和稳定的 Agent 身份为键，不以 Session 或 Agent 版本为键。已有
Session 操作走同一 provider，此时携带 Session id。创建会话与接收消息重新检查
准备状态；执行仍经过原来的准备钩子，provider 必须检查真实授权，包括接受后撤销。

问卷或审批保留真实 interaction id 和公开 contract，按 contract 提交 answer。
回答和停止均复用核心接口，回执不是执行完成。响应 `status` 原样使用 `running`、
`waiting`、`ended`，`outcome` 原样使用 `completed`、`failed`、`cancelled`。
输入被拒绝仍是失败的 Input，不包装成成功的助手回复。这里没有新增 Agent 执行循环。

## 附件

1. **上传**：`POST /v1/upload.create`，请求头同上，`multipart/form-data`。每个
   part 是一个文件，带唯一的 `name`、`filename` 和 `Content-Type`。响应把每个 part
   名映射为链接 `internal:astrabox/{agent_id}/_tmp/{id}`。链接归上传人在这个端点
   使用，15 分钟内有效，被消息发送后失效。上传不需要 Channel，也不需要沙箱在运行。
2. **发送**：把链接和文字一起写进 `message.create` 的 content：
   `对比一下 <img src="..."/><file src="..." title="report.csv"/>`。`<img>` 把文件
   作为图片交给模型，必须是 PNG、JPEG、GIF 或 WebP；`<file>` 可以是任意文件。每个
   文件还会在引擎读这条消息前写进 Channel 工作区的 `uploads/{input_id}/{文件名}`，
   消息里会告诉引擎这些路径。一条消息最多 10 个文件，每个不超过 25 MiB；一次上传
   最多 10 个 part。
3. **回读**：历史和 `message-updated` 的 content 用
   `internal:astrabox/{agent_id}/{channel_id}/attachments/{id}` 列出用户发送的文件，
   内部历史还在 `metadata.attachments` 中给出。这些链接在 Session 存续期间都能读到
   保存的文件。助手回复列出它的工具报告改动过的工作区文件，链接为
   `internal:astrabox/{agent_id}/{channel_id}/files/{路径}`；agent 用其他方式（脚本、
   Bash 命令）生成的文件用 `astrabox.files.list` 查找。工作区链接在 Channel 工作区
   存在期间可读。
4. **下载**：`GET /v1/proxy/{链接}`，带调用人的 `Authorization`，返回文件字节和
   媒体类型。`_tmp` 链接只有上传人能读，其余链接只有 Channel 所有者能读。本服务只
   代理自己的 `internal:` 链接，不公布 `proxy_urls`。

| 错误码 | 状态 | 含义 |
| --- | --- | --- |
| `INPUT_ATTACHMENT_UNSUPPORTED` | 400 | 文本、`img`、`file` 以外的元素；不是 `upload.create` 返回的链接；`img` 不是受支持的图片 |
| `INPUT_ATTACHMENT_TOO_LARGE` | 413 | 文件超过 25 MiB |
| `INPUT_ATTACHMENT_UNAVAILABLE` | 409 | 上传已过期、已被另一条消息发送，或由其他调用人、其他端点上传；重新上传 |
| `ENGINE_CAPABILITY_UNAVAILABLE` | 409 | 该 Agent 的引擎不接受图片输入；改用 `<file>` 发送 |

消息被接受时 Session 运行时尚未启动的，`ENGINE_CAPABILITY_UNAVAILABLE` 会记录为
该输入的失败原因。文件未能写进工作区时，输入记录为 `INPUT_ATTACHMENT_DELIVERY_FAILED`，
引擎没有读到这条消息。用同一 `Idempotency-Key` 重试 `message.create` 返回原输入，
不会再次发送文件。

## 事件与重连

WebSocket 连接 `/v1/events`，发送 `{op: 3, body: {token, sn?}}`。token 仍经部署方
身份 provider 验证。READY 的 op 为 4，包含目标 Agent 的 Login。至少每十秒发一次
PING（op 1），服务端回 PONG（op 2）。建立连接时检查身份、Environment 进入权限与 Agent 权限；每个 HTTP 请求重新解析
当前身份和权限。

EVENT（op 0）的 body 带当前执行人／Agent 专属的递增 `sn`：

- `message-created` 表示助手响应开始，`message-updated` 在核心流片段结束时给出
  累积文本。片段结束也可能是等待交互，不能单凭它认定执行完成。
- `internal` 的 `_type=astrabox.output`，`_data` 包含 `session_id`、`response_id`、
  `first_seq`、`through_seq`、`parts`；parts 是原来的公开 UI chunks，保留原生消息
  位置、错误和真实交互标识。
- `internal` 的 `_type=astrabox.state`，携带观测到的核心 Session 状态、运行状态和
  最近响应的状态／结果。断线期间的中间状态观测会合并；完整历史保留更早响应的结果。

处理完成后保存 sn；重连传该值，补发可见事件中序号更大的部分。不传 sn 只接收后续
事件，传 0 包含仍保留的历史事件。游标只属于这个执行人和 Agent，不能跨身份使用，
超前游标会拒绝。客户端若已处理但未保存游标，按 sn 忽略重复事件。删除 Session
会移除其补发索引，因此序号允许有空隙。

所有副本使用共享数据库。序号、输出位置与事件引用在同一行锁事务中提交；没有进程
内补发缓存或新的执行队列。事件引用核心不可变输出日志，保留期跟随 Session 记录，
不依赖持久工作区卷。sn 是跨 Channel 的投递顺序；对话展示使用完整历史及位置
chunks 中的原生消息顺序，不能按用户提交时间排序。

## 部署及示例

现有 AstraBox 服务承载 HTTP／WebSocket 路径。反向代理转发两者，每个副本安装
相同身份和准备 provider，使用同一 Environment 数据库和正常的协调配置。若显式
覆盖 `ASTRABOX_AUTH_EXEMPT_PREFIXES`，须包含 `/api/satori/`；入口自行验证 HTTP
和 IDENTIFY 凭据。共享 Docker 沙箱服务的副本还须使用相同的
`ASTRABOX_MCP_PROXY_BASE_URL`、`ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM`、
`ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS`，分别约束回调、DNS 和网络策略。独立 Web
副本应取得与托管启动器派生结果一致的值；网络指纹不同会正确拒绝使用这些沙箱。
凭据只保留在业务后端；外部浏览器仍可只访问业务系统。

`examples/satori-agent/client.py` 可执行授权状态查询、创建会话、幂等提交与事件接收；
未授权或待交互时明确显示状态。运行方法见示例目录。

使用 Satori Python core 1.3.11，以及 server 1.3.11 中未经修改、MIT 授权的
WebsocketConnection；来源和许可在 `astrabox/vendor/`。不提供社交／群组管理、
webhook、授权页面或内部员工系统适配，不改变原有渠道客户端和供应商 Agent Runtime。
