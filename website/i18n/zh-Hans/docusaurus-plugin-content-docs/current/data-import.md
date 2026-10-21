# 离线升级时保留记录

启动升级后的安装之前，将现有数据转换为**当前检出版本的 PostgreSQL 结构，Alembic 修订
`0003`**。本指南定义需要保留的记录关系和状态。转换、事务加载与校验由运维方负责；
AstraBox 不提供整套安装的导入命令，也不会自动转换 0.1.8 记录。其他数据库和私有扩展格式
不在本规范范围内。

以下示例已对照当前模型和 API 检查，但未作为迁移验收执行。固定安装版本及其数据库修订，
不要把旧集合文档直接放入新表。导入时将 `sessions.runtime_versions` 保持 NULL：
启动后的运行时连接会记录实际版本，以及对应的沙箱和运行实例。

## 准备与加载顺序

1. 在一致的时间点停止源端与目标端写入、调度和运行时读取。完成原生状态保存，导出前处理
   输入是否已被取走的不确定情况。备份源数据和密钥；工作区文件通过其存储系统单独保留。
2. 用发布版本的离线 `astrabox.persistence.sql.migrate.upgrade_to_head()` 准备目标
   PostgreSQL 结构。这只应用结构修订，不转换旧数据。不要为准备导入而启动单容器部署或 API：
   正常启动会在扩展生命周期钩子之前创建安装标识、核对计算资源、初始化默认记录并启动读取方。
3. 在运维方控制的数据库事务中，依次加载安装标识与密封凭证、Environment、Agent 和
   Assistant 及其鉴权。先加载 Session，再设置 Assistant 主 Session 关联；同时保留所选
   记录使用的有序 Vault、MCP 和 connector 绑定。
4. 加载选定历史；需要原生续接时，加载完整原生状态。提交前校验记录关系与密文。
   下述 HTTP 历史接口在启动后使用独立事务，只适用于 Session 尚无自身消息的情况。
5. 离线事务提交、原始标识与密钥配置到位后，才能启动安装。发送第一条有意新增的输入之前，
   验证所有权与历史读取。

非空目标可能包含相同引用或无关记录。加载器必须比较规范化值：同一 ID 与内容保持不变；
同一 ID 对应不同所有者或配置属于冲突，应回滚加载。不要盲目 upsert、重命名业务 ID 来
绕过冲突，或在未检查结果前重试不确定的提交。这些是加载器要求，不是内置记录关系导入 API。

## 标识与字段对应示例

以下虚构值展示需要保留的关联，是字段对应表，不是可执行 SQL 或新输入格式。
转换后的记录应携带全部必需配置、有效生命周期字段与时间戳；默认值和可空性以相应发布模型为准。

| 表 | 示例字段 |
|---|---|
| `installation` | `singleton=true`，`installation_id=<原安装 ID>` |
| `secrets` | `scope='environment/agent-env'`，`key='provider_access.api_key/ref-01'`，`sealed=<原认证密文>` |
| `environments` | `name='agent-env'`，`engine_kind='claude_code'`，`network_type='unrestricted'`，`idle_action='pause'`，`provider_api_key_ref='provider_access.api_key/ref-01'`；另一个 `assistant-env` 使用已安装且支持 Assistant 的 `hermes` 程序 |
| `agents` | `agent_id='agent-01'`，`name='Example'`，`name_key='example'`，`environment_name='agent-env'`，`model=<配置的模型>`，`created_by='user-01'`，`org_id='org-01'`，`visibility='allowlist'`，`admins=['user-01']`，`allowed_user_ids=['user-01']`，`version=1`，`state='ACTIVE'` |
| `assistants` | `assistant_id='assistant-01'`，`owner_id='user-01'`，`org_id='org-01'`，`engine_kind='hermes'`，`environment_name='assistant-env'`，`display_name='Workspace'`，`is_primary=true`，`workspace_id=<保留的工作区 ID>`，起初 `main_session_id=NULL` |
| `sessions` — Agent | `session_id='00000000-0000-4000-8000-000000000001'`，`user_id='user-01'`，`org_id='org-01'`，`session_kind='agent_chat'`，`workspace_kind='agent'`，`agent_id='agent-01'`，`assistant_id=NULL` |
| `sessions` — Assistant | 独立的稳定 Session ID，以 `002` 结尾，相同 user/org，`session_kind='assistant_chat'`，`workspace_kind='assistant'`，`assistant_id='assistant-01'`，`agent_id=NULL` |
| 主 Session 关联 | Session002 存在后，将 `assistants.main_session_id` 设为它，验证所有者与 Assistant ID 一致 |

精确的发布模型见
[Environment](https://github.com/Colton-z/AstraBox/blob/v0.1.10/astrabox/persistence/sql/models/environments.py)、
[Agent](https://github.com/Colton-z/AstraBox/blob/v0.1.10/astrabox/persistence/sql/models/agents.py)、
[Assistant](https://github.com/Colton-z/AstraBox/blob/v0.1.10/astrabox/persistence/sql/models/assistants.py)、
[Session 与原生状态](https://github.com/Colton-z/AstraBox/blob/v0.1.10/astrabox/persistence/sql/models/sessions.py)及
[Vault/Secret](https://github.com/Colton-z/AstraBox/blob/v0.1.10/astrabox/persistence/sql/models/vaults.py)。
约束不会检查每一种语义关联：还需验证 Session 类型、程序兼容性、同一 Session 内的回复与
输入关联，以及 Assistant 仅归属用户可访问。内部标识列需要正确恢复分配器，或进行保留引用的
重映射；公开输出游标和业务 ID 必须保持稳定。

正常创建 Agent 会生成 ID 并安排状态核对，不能代替保留标识的离线加载。
存在已删除的主 Assistant 记录时也应保留，它们参与主身份历史。启动前还需考虑默认初始化：
任意存活 Agent 都会阻止默认初始化；只有 Assistant 的数据库不会。没有存活 Agent 时，
初始化可能替换名为 `claude-code` 的 Environment。不要伪造 Agent 绕过这个行为，也不要
假定只导入 Assistant 就能阻止默认初始化。

## 通过现有 API 导入早期消息

经过认证的管理员可以在 Session001 产生自身消息之前添加展示历史。使用实际保留的 Session ID：

```http
POST /api/v1/admin/sessions/00000000-0000-4000-8000-000000000001/history-import
Content-Type: application/json
Authorization: Bearer <administrator access token>

{"messages":[
  {"source_id":"old-user-1","role":"user","created_at":"2026-10-01T10:00:00Z","blocks":[{"type":"text","text":"Earlier question."}]},
  {"source_id":"old-reply-1","role":"assistant","created_at":"2026-10-01T10:01:00Z","parent_source_id":"old-user-1","completion":"complete","blocks":[{"type":"text","text":"Earlier answer."}]}
]}
```

首次导入在普通响应封装中返回 `imported: 2, unchanged: 0`；完全相同的再次导入返回
`imported: 0, unchanged: 2`。修改 `old-reply-1` 对应回复会得到 `HISTORY_IMPORT_CONFLICT`；
重新排列已导入消息也会冲突。Session 已产生自身消息时以 `HISTORY_IMPORT_AFTER_CONVERSATION` 拒绝。

API 也保留支持的图片、思考、工具调用和工具结果块。源记录没有回复结束信息时使用
`completion: "unknown"`，不要制造完成状态。API 分配新的导入输入/回复 ID，并保留来源。
它不执行导入输入，也不恢复原生 SessionStore。参阅[导入早期历史](sessions.md#import-earlier-history)。

## 保留身份、密钥与原生状态

平台用户 ID 跟随身份提供方的稳定 subject；显示名称记录不注册账户，也不授权访问。
保留身份提供方配置、user/org ID、Agent 鉴权与 Assistant 所有权。外部 Agent 账户、委派和
文件系统映射仍由扩展方负责：提供稳定 ID、凭证/刷新引用、工作区路径与 ACL。
平台用户 ID 不会重建外部账户，不应要求重新授权来掩盖缺失的映射。

只有原 Secret Store 格式、scope/key、主密钥或 KMS 解密权限与上下文齐全时才复制密文。
在私有环境中验证每个引用值；掩码、缺失密钥和无法解密的值都属于错误。
切换存储时需要通过目标存储显式重新密封，同时保留平台签名材料。复制记录不会修复过期或撤销的外部授权。

只有替换同一个已停止的安装时，才保留安装 ID。不要覆盖其他安装的 ID 来接管它的沙箱。
通过原所有者的生命周期退役计算资源，或在切换后显式重建；不要移植活动读取方/终端租约、
待处理控制代次或预热领取记录。工作区标识与文件独立保留。

需要原生续接时，保留 `engine_session_key`、每个 transcript scope 与原生子对话记录，
以及程序使用的原生 SessionStore 和 profile-state 归档。校验所有者组合、已发布头、前序关系、
块数与顺序、字节大小及 SHA256。保留输出 part、交互与已保存回答，而不只是文本。
缺少这些状态时，只能称为可读历史加新的原生执行，不能称为上下文恢复。
拒绝无法确定输入是否已被取走或不兼容的游标映射；已取走的工作不得重新变成待处理，
也不得为重建缺失历史而重放。
