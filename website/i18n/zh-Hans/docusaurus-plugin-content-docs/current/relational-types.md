# 关系型结构映射

共享 SQLAlchemy 模型保留 PostgreSQL 的文本键、带时区时间戳、JSONB、文本数组和 BYTEA。
`astrabox/persistence/sql/types.py` 也声明了 MySQL 8.4 的物理表示。这些是数据库结构的
基础，不是完整 MySQL 后端支持。仓库表达式、结构初始化与物理协调仍需兼容实现和真实验收。
特别是 MySQL 的 Agent/Assistant Environment 外键，要求在 Environment 删除操作的共享事务中
清理已删除记录的引用，详见下文。MySQL DDL 能编译，不代表支持 OceanBase 或其他 MySQL 兼容服务。

## 值

`UTCDateTime` 使用 PostgreSQL TIMESTAMP WITH TIME ZONE 和 MySQL DATETIME(6)。
MySQL 绑定前将带时区值转换为 UTC，返回值携带 UTC；无时区值按 UTC 解释。
提供方必须建立 UTC 数据库会话；数据库时钟表达式使用共享 `func.now()` 编译器，
不以应用时钟代替，也不使用自动 ON UPDATE 时间戳。

`payload_json()` 保留原生 JSON，Python `None` 存为 SQL NULL；`JSON.NULL` 仍是显式
JSON null。空数组、空对象与缺失各不相同。`StringList` 使用 PostgreSQL 文本数组和
MySQL JSON 数组，保留顺序、重复项和 null 元素。MySQL 绑定校验与 JSON_SCHEMA_VALID
约束拒绝其他元素类型；服务输入校验可要求不含 null 的字符串。MySQL 空数组默认值用原生
JSON_ARRAY() 表达式，空对象用 JSON_OBJECT()。长文本常量默认值按 MySQL 要求加括号。
共享 Python float 注解在 MySQL 使用 DOUBLE，而不是单宽 FLOAT；PostgreSQL 保留 FLOAT 编译。

未索引文本通过 `payload_text()` 映射为 MySQL LONGTEXT；原生归档字节通过
`binary_payload()` 使用 LONGBLOB。快照仓库的 256 KiB 块超过 MySQL BLOB 容量。
这些物理类型不会覆盖驱动包大小或服务端资源限制。

## MySQL 键限制

`KeyText` 在 PostgreSQL 保留 Text，不增加长度限制；在 MySQL 使用包含完整 UTF-8 值的
VARBINARY，区分大小写、尾随空格与每个字节，不截断、规范化或用哈希替换键。
以下是后端物理分配，不是全产品身份长度上限。拒绝本来有效的身份仍然限制参考后端的能力，
不能由这些分配推断所有开发者的外部或原生身份都能容纳。

| 字段 | 最大 UTF-8 字节数 | 当前生成方与调用方 |
|---|---:|---|
| Session、输入、终端命令 ID；读取方所有者 | 128 | Session/输入通常为 UUID；终端为 `tcmd_` 加 32 位十六进制；读取方为 hostname:PID:8hex。显式导入可更长。 |
| Environment 名称与 Secret 键引用 | 512 | Environment 无全局上限；凭证 ID 为 `vcr_` 加 32 位十六进制；环境 Secret 键含字段名与 UUID 后缀。 |
| Secret scope | 768 | 包含 `environment/` 加 Environment 名称，或 Vault/Deployment 标识。 |
| Secret key | 512 | 凭证/字段标识，包括生成的 Secret 键。 |
| Session 输入客户端消息 ID | 1024 | API 接收调用方字符串，没有全局上限；默认通常是输入 UUID。 |
| 输入状态与终端状态 | 32 | 当前生命周期代码，包括 `delivered` 与 `interrupted`，可容纳。 |
| 其他生成实体 ID 与匹配外键 | 128 | Agent、Assistant、Deployment、Artifact、Vault、沙箱、slot、control、card、credential、reservation 与 token；任意导入或提供方标识可能超长。 |
| 用户、组织、所有者与执行身份 | 512 | 外部身份提供方没有通用字节上限。 |
| 目录提供方 / 目录项 / connector ID / 折叠名称 | 128 / 1024 / 512 / 512 | 外部 ID 可能更长；Unicode casefold 可能扩展 Agent 名称。 |
| 渠道对话、去重、别名、参与者、引用和 outbox 回复键 | 1024 | 原生身份与派生路由/回复键无通用上限。 |
| 渠道消息时间戳 / 绑定修订 | 128 / 64 | 提供方时间戳文本 / 平台 ISO 时间戳。 |
| Artifact 文档路径与集合 | 各 1024 | 斜线路径语法无全局字节上限，深层或多字节路径可能超长；PG 保留显式 C 排序规则。 |
| 对话记录项目 / 原生 Session / 子路径 / 追加标识 | 1024 / 512 / 1024 / 128 | 原生供应方未普遍保证这些边界；超长必须明确拒绝，不能静默遗漏原生历史。 |
| 凭证匹配 URL / Secret 名称 | 2048 / 512 | 有效 URL 与名称可能超过参考分配。 |
| 快照所有者与快照摘要 / 归档类型 | 64 / 32 | SHA256 十六进制身份与平台归档枚举。 |
| Artifact 预算 / 查看者 / 任务引用 | 1024 / 512 / 1024 | 当前预算/查看者用有界 HMAC 身份，导入或调用方派生值可能更长。 |
| 后端 / 池 / profile / 作业 / profile 标记 / 预留键 | 128 / 512 / 1024 / 512 / 512 / 512 | 提供方与运行时字符串无通用长度保证。 |

模型声明定义每列与匹配外键的精确分配，不定义通用的提供方身份格式。不同物理表示需要独立
评审与验收。匹配外键使用相同二进制表示和长度；Secret 复合键最多 1280 字节，输入去重键
最多 1152 字节。选定表使用 InnoDB DYNAMIC 行格式；MySQL 必须提供 3072 字节索引预算
（16 KiB 页）。写入要求严格 SQL 模式，但它不能代替应用侧校验。

有类型的 SQLAlchemy 值发送前调用 `KeyText` 校验。导入方必须在首笔写入前，对完整具体批次
调用 `validate_backend_keys(table, rows, dialect)`。INSERT FROM SELECT 必须为每个键
字面量赋予目标列类型，或提前校验具体值。原始 SQL 和无类型表达式绕过绑定检查，不能声称
具有相同拒绝保证。

校验器使用所选方言的适配绑定处理器。独立提供方可以通过 SQLAlchemy 扩展结构与类型/编译器，
但必须一同保留完整键相等性、约束与绑定行为。参考宽度不是新的全局提供方 API，也不要求特定驱动。

PostgreSQL 中有效的值可能超出这些分配，例如 `"研发" * 86` 是 516 字节的非空 Environment
名称，外部客户端消息 ID 可超过 1024 字节，显式 Session ID 可超过 128 字节。
MySQL 必须在写入或导入前拒绝；不能改名、截断，也不能把 MySQL 限制施加给 PostgreSQL。

实际生成方位于 `astrabox/core/service/orchestrator/` 下的 `session_lifecycle/service.py`、
`session_runtime/service.py` 和 `session_runtime/host.py`；凭证 ID 在
`astrabox/persistence/repository/vault_repository.py` 生成；外部请求类型在
`astrabox/api/routes/sessions.py`。

## 顺序与条件唯一性

Session 输入顺序仍由数据库生成。PostgreSQL 保留非主键 Identity(always=True)；MySQL
使用非主键 AUTO_INCREMENT 及其必需的内联非唯一序列索引。显式指定序列由共享仓库/导入方
负责：MySQL 不强制 PostgreSQL 的 GENERATED ALWAYS 限制。在同一事务中通过业务键读取
生成行，不使用 MySQL 不支持的 RETURNING。

终端的单条运行命令约束在两种方言使用相同状态条件。PostgreSQL 保留部分唯一索引；MySQL
创建 STORED 生成标记，运行时为 1，否则 NULL，再添加 UNIQUE(session_id, marker)。
生成列与约束在同一 CREATE TABLE 中；标记不是共享返回字段。不活跃行不受限制，活跃重复项
原子失败。MySQL 输入状态索引包含已关闭记录，不依赖不支持的部分索引；这是性能索引，不是唯一性规则。

## Environment 引用

Agent 与 Assistant 的存活 Environment CHECK 在两种方言都强制执行。PostgreSQL 保留
ON DELETE SET NULL；只有这两个外键在 MySQL 编译为 ON DELETE RESTRICT，因为其 CHECK
校验器无法验证存储引擎的 SET NULL 操作。其他外键动作和逻辑名保持一致。参阅
[精确外键校验](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_table.cc#L6325-L6342)。

共享的 Environment 删除操作必须持有 Environment 锁，拒绝存活或仍占用计算资源的引用，
只清理符合条件的已删除 Agent/Assistant 引用，再在相同事务中删除 Environment 和清理 Secret。
MySQL 结构要求这个清理流程，编译结构本身不会提供它。RESTRICT 拒绝残留引用，CHECK 防止
存活定义缺少 Environment。存在已删除引用时直接执行 SQL DELETE 与 PostgreSQL 不同，
不能称为等价于共享删除操作。

这些选择使用 SQLAlchemy 2.0.52 原生类型与编译器钩子。参考约定：
[SQLAlchemy UTC 类型](https://docs.sqlalchemy.org/en/20/core/custom_types.html#store-timezone-aware-timestamps-as-timezone-naive-utc)、
[MySQL 8.4 索引限制](https://dev.mysql.com/doc/refman/8.4/en/innodb-limits.html)、
[生成列](https://dev.mysql.com/doc/refman/8.4/en/create-table-generated-columns.html)、
[JSON 结构校验](https://dev.mysql.com/doc/refman/8.4/en/json-validation-functions.html)和
[表达式默认值](https://dev.mysql.com/doc/refman/8.4/en/data-type-defaults.html)。
