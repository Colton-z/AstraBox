# 关系型持久化提供方

关系型提供方通过 SQLAlchemy 提供数据库物理服务。AstraBox 在平台中保留仓库语句、业务条件
和事务流程；提供方不替换业务仓库，也不解释运行时事实。

内置 `postgres` 提供方使用 PostgreSQL 和 asyncpg。选择外部提供方或使用可复用的 MySQL
语句组件，本身不代表已有完整、受支持的后端。声明支持前，应验证实际数据库、方言、结构和协调实现。

## 注册与选择提供方

在独立 Python 分发包中发布提供方，注册其类：

```toml
[project.entry-points."astrabox.providers.persistence"]
example = "example_storage:ExamplePersistence"
```

设置 `ASTRABOX_PERSISTENCE_PROVIDER=example`，并将 `ASTRABOX_DB_URL` 设为该提供方的
原生 URL。提供方类接收关键字参数 `database_url: str`。AstraBox 只加载选中的持久化入口；
名称未知或缺少必要操作时，在初始化数据库结构前报错，不会静默替换提供方。

实现 `astrabox.seams.relational_persistence.RelationalPersistenceProvider`：

| 成员 | 职责 |
|---|---|
| `database_url` | 由提供方规范化的原生配置 URL。 |
| `get_engine()` | 对同一事件循环与配置返回相同的 SQLAlchemy `AsyncEngine`；共享操作收到的连接来自该引擎。 |
| `statements` | 下述三项原生冲突操作。 |
| `coordination` | 运行时所有权、绑定提交的通知、监听与重新同步。 |
| `initialize_schema()` | 初始化当前构建的数据库结构，并串行协调同时启动的副本。 |
| `translate_error(error)` | 按实际约束标识分类原生完整性错误；未识别的错误原样返回。 |
| `pool_report(holders=5)` | 不新建连接、不暴露凭证的诊断。 |
| `dispose()` | 使用方停止后，释放当前事件循环的提供方资源。 |

数据库实际约定允许时，使用持续维护的 SQLAlchemy 异步方言。驱动依赖、引擎和连接池设置
由提供方负责。协调组件管理其保证所需资源的容量与生命周期；没有公开的引擎池选择开关。
每次调用都返回新引擎违反稳定引擎约定。

## 原生冲突操作

`RelationalStatements` 只有以下三项操作。`astrabox.persistence.sql.statements` 中的共享
函数校验目标与值的约定，再调用选中提供方的 `statements` 成员。

- `upsert_primary_key(connection, table, rows, *, update_columns, update_values=None)`
  构造真正针对主键的 SQLAlchemy `Insert`。其他唯一性错误必须保留含义；更新值仍为普通
  SQLAlchemy 值或表达式。
- `insert_if_absent_primary_key(connection, table, rows)` 为没有其他竞争唯一键的表构造
  插入或原生无操作冲突语句。不承诺插入/已存在结果或行数；调用方先执行，再做自己的加锁读取。
- `await insert_if_absent(connection, table, values, *, index_columns, where=None)`
  在调用方现有事务中执行。插入返回 `True`；没有符合条件的行，或能证明冲突恰好针对所请求
  目标时返回 `False`。提供的 SQLAlchemy `where` 表达式保留在插入内部，不得替换为单独的资格预锁。

共享校验接受普通、无条件的唯一列目标与具体目标值，保留 NULL-distinct 语义。
部分、函数、前缀和 NULLS-NOT-DISTINCT 目标需要单独评审的实现。目标处存在一行并不足以
抑制完整性错误：实际违反的约束必须匹配。未知或无关错误应继续抛出。重复读取不等于独占所有权。

`astrabox.providers.relational_statements` 中的 `PostgresStatements` 和 `MySQLStatements`
是可复用的物理实现。存在可能扩大冲突目标的其他唯一约束或索引时，MySQL 组件拒绝主键 upsert。
需要明确结果的插入使用保存点并确认精确约束；只针对主键的无操作 upsert 避免在后续需要写排他
时升级共享锁。这两个组件都不提供数据库结构、驱动、调度器或协调服务。

独立命名的 SQLAlchemy 方言可以实现自己的 `RelationalStatements`，无需修改共享函数。
类型和仅表达式差异，包括 `TimestampAfter`，使用 SQLAlchemy 现有方言编译扩展；不要引入
另一套查询语言或数据库执行器。物理类型限制见[关系型结构映射](../relational-types.md)。

## 事务与协调保证

每项操作使用提供的实际 `AsyncConnection`。提供方不得自行提交、切换数据库、重试业务回调，
或把 COMMIT 响应丢失视为回滚证据。共享操作保留原子条件、输入去重、读取方隔离和完整原生
SessionStore 保存要求。

`coordination.notify(connection, notice)` 在相同连接、COMMIT 之前登记通知意图。
回滚不得发布已提交状态通知；业务服务在提交后另发通知存在崩溃间隙，并不等价。
订阅生效后才能报告监听就绪；重连间隙需要报告给使用方，以便重新读取持久状态。
运行时所有权必须保留排他性、丢失检测行为和交付的所有权检查；ping 成功或仅有数据库引擎
不足以证明这些保证。

数据库结构、类型映射、约束、隔离和数据库时间必须支持共享操作。内置 DBOS 调度器独立
要求 PostgreSQL；选择持久化提供方不会自动使调度器可移植。驱动或编译器接受语句，不等于
后端已经通过端到端验收。
