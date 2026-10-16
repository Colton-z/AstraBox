# 接入新的调度器

`SchedulerProvider` 负责运行定时 Deployment：在每个到期时刻触发，为每次调用分配持久的
运行记录 ID，回调 AstraBox 启动这次运行的 Session 并接收它的输入，负责重试和恢复中断的
调用，并保存控制台列出和重放的运行记录。

AstraBox 内置一个 provider：`dbos`。它在每个服务进程中嵌入 DBOS Transact，把状态保存在
AstraBox 已使用的 PostgreSQL 数据库中。部署需要其他执行引擎时，再编写新的 provider。

## AstraBox 与 provider 的分工

| AstraBox | provider |
| --- | --- |
| Deployment 记录：名称、提示词、Cron 表达式、IANA 时区和启用状态 | 按 Schedule 的时区在每个到期时刻触发 |
| 谁可以创建、修改、立即运行或重放 Schedule | 每个到期时刻在所有副本中只产生一个持久的运行记录 ID |
| 以运行记录 ID 幂等地启动这次运行的 Session 并接收它的输入 | 持续回调 AstraBox，直到运行成功或永久失败 |
| 对外的运行状态：以 Session 对这次运行输入的回复为准，没有回复时为 provider 的分派状态 | 重试、恢复中断的调用，以及运行记录台账 |

Deployment 记录始终是期望状态。AstraBox 启动时把所有已保存的 Schedule 交给 provider
（`reconcile`）；之后每次修改只交给发生变化的那一个（`apply` 或 `delete`）。provider
原样保存 AstraBox 给它的载荷（目标和提示词），并在每次调用时交还，因此修改 Schedule
不会改写已经存在的运行记录。

## provider 必须保证的行为

1. **到期时刻。**按 Schedule 的 IANA 时区和五段 Cron 表达式计算每个到期时刻。没有
   AstraBox 进程运行时，或 Schedule 处于停用状态时错过的时刻直接跳过，不补跑，也不并入
   后面的运行。即使前一次运行尚未结束，每个到期时刻也是独立的一次运行。
2. **每个到期时刻只运行一次。**同一个 Deployment 的同一个到期时刻，在所有副本中只产生一条
   运行记录。用 provider 存储中的唯一约束保证这一点，而不是先读后写。
3. **分派。**每次运行先调用 `RunHandler.start_session`，持久保存返回的 Session ID，
   再调用 `RunHandler.admit_input`，并把它返回的 `RunInputBinding` 中的输入 ID 保存为
   这次运行的 `input_id`。两个调用都可能用同一个 `RunInvocation` 重复执行；AstraBox
   按运行记录 ID 保证幂等。其他失败按有上限的退避重试；遇到 `RunPermanentError` 或
   重试次数用尽时，把运行记录标记为 `FAILED`。
4. **恢复。**因进程丢失而中断的运行，以同一个运行记录 ID 和已经保存的 Session ID 继续。
5. **停用。**以停用的定义调用 `apply` 并返回后，最迟在 provider 声明的
   `pause_visibility_seconds` 之后不再启动新的定时运行；已经开始的运行继续执行。
6. **立即运行和重放。**`enqueue` 在运行记录保存后返回，不会挪动后续的到期时刻。
7. **运行历史。**`list_runs` 按从新到旧的顺序返回一个 Deployment 所有触发方式的运行记录。
8. **失败。**存储或引擎故障时抛出 `SchedulerUnavailable`，引擎拒绝表达式时抛出
   `InvalidSchedule`，超出引擎上限时抛出 `ScheduleCapacityExceeded`。引擎自己的术语和
   错误文本不会出现在 API 响应中。

完整契约和所有类型见 `astrabox/seams/scheduler.py`。

## 声明引擎特性

不同引擎在三个方面确实不同，需要在 `SchedulerProperties` 中声明；AstraBox 会在启动时
记录到日志。

| 特性 | 含义 | `dbos` |
| --- | --- | --- |
| `recovery_scope` | `"cluster"` 表示任一副本都可以接手进程丢失后留下的运行；`"executor"` 表示只有以相同身份重启的进程才会接手。 | `"executor"` |
| `max_active_schedules` | 引擎支持的启用 Schedule 上限，没有上限时为 `None`。超过上限时 `apply` 会拒绝。 | `64` |
| `pause_visibility_seconds` | 停用后 Schedule 最多还会触发多久。 | `11` |

## 检查部署配置

AstraBox 启动时调用 `configure(database_url=...)`，传入平台的 PostgreSQL URL，provider
可以用它保存自己的持久状态。provider 无法在这个部署中运行时，抛出
`SchedulerConfigurationError` 并说明缺少什么：AstraBox 会拒绝启动，并提示运维人员选择
其他 provider，或设置 `ASTRABOX_SCHEDULER_PROVIDER=none`。`dbos` provider 把定时计划
和运行记录台账保存在该数据库的 `dbos` schema 中。

`start(handler)` 开始分派，并恢复进程丢失后留下的运行；`stop()` 在 AstraBox 服务仍在
运行时，于有限时间内完成排空。

## 在文档数据库上实现这些保证

基于文档数据库的 provider 可以用自己的集合这样实现上述保证：

- 每次调用保存一个文档，在 Deployment ID 和 UTC 到期时刻上建唯一索引。到达同一到期
  时刻的每个副本都会插入这个文档，唯一索引保证只有一次插入成功。
- 用带条件的更新获取调用的处理权并写入租约到期时间，处理器运行期间持续续租。发现租约已过期的
  进程以同一个运行记录 ID 接手这次调用，这就是 `recovery_scope="cluster"`。
- 在调用 `admit_input` 之前保存 Session ID，接手的进程才能复用它。如果租约过期时第一个
  持有者仍在运行，重复的调用由 AstraBox 按运行记录 ID 的幂等性吸收。

## 打包 provider

在 provider 的 Python 发行包中声明：

```toml
[project.entry-points."astrabox.providers.scheduler"]
example = "astrabox_scheduler_example:ExampleSchedulerProvider"
```

把类的 `name` 设为 entry point 名称。部署通过 `ASTRABOX_SCHEDULER_PROVIDER=example`
选择它；`none` 保留给不运行定时 Deployment 的部署。AstraBox 启动时实例化并注册这个类。
类也可以设置 `seams_api_version`；声明的版本不兼容时，服务在启动阶段失败，而不是等到
第一个到期时刻才失败。

## 验证 provider

在 provider 的测试中绑定可复用的契约测试：

```python
from astrabox.testing.scheduler_conformance import SchedulerProviderContractSuite

from astrabox_scheduler_example import ExampleSchedulerProvider


class TestExampleScheduler(SchedulerProviderContractSuite):
    def make_provider(self, store):
        provider = ExampleSchedulerProvider()
        provider.configure(database_url=...)  # 为 `store` 准备的全新数据库
        return provider

    async def fire_due_times(self, provider, deployment_id, start, end):
        ...  # 按引擎自己的方式触发 (start, end) 之间的每个到期时刻
```

这组测试在单个进程中运行，覆盖通过 handler 分派、永久失败和临时失败、运行历史、运行
保存的载荷、重放来源、同一到期时刻触发两次仍只有一次运行，以及删除 Schedule。另外两项
保证需要多个进程——另一个副本触发同一个到期时刻，以及进程消失后恢复运行——需要在
provider 存储的多副本部署上验证。

然后在真实部署上走完一个 Schedule 的完整流程：创建、等待到期时刻触发、选择**立即运行**
和**重放**、停用，最后删除。

## 相关文档

- [定时运行任务](schedules.md)——从用户角度使用 Schedule
- [自动运行 Agent](deployments.md)——Deployment 及其运行记录
- [嵌入 AstraBox](embedding.md)——entry point 和启动过程
