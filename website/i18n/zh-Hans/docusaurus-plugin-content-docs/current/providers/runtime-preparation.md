# 运行准备集成

通过 `astrabox.providers.runtime_preparation` 安装一个由部署提供的
`RuntimePreparationProvider`：

```toml
[project.entry-points."astrabox.providers.runtime_preparation"]
enterprise = "my_deployment.preparation:PreparationProvider"
```

入口点指向 `astrabox.seams.RuntimePreparationProvider` 的一个子类，该子类带有稳定的
`name`。安装了相互冲突的 provider 时，服务启动失败。未安装 provider 时，平台准备 Agent
和 Session 时不进行任何回调。

部署准备 Agent、Assistant 或 Session 的每个时点，都由同一个 provider 响应。每个方法
都有默认实现，只需实现你需要的时点：

| 方法 | 平台何时调用 |
| --- | --- |
| `plan` | 每次为 Agent 规划运行时；同步调用，不执行 I/O |
| `synchronize`、`recover` | 每个预先准备的 Agent 沙箱一次，在它成为待领取库存之前；对 Assistant，在其工作区每次准备一个修订版本时 |
| `prepare_session` | 引擎初始化之前：冷启动、领取、重新接入、替换沙箱 |
| `prepare_turn` | 每条输入交给引擎之前 |
| `read_user_actions`、`perform_user_action` | Session 所有者读取或处理其前置条件时 |

顺序、放置、写入隔离（fencing）、文件投递和失败处理由平台负责；外部身份、声明格式、
凭证策略和前置条件由 provider 负责。provider 从不自行发起授权同意流程，不按 Session
注册 Agent，也不分派工作。

## 声明何时需要 Session

`plan(*, config) -> RuntimePreparationPlan` 是同步方法，不执行 I/O。它收到的已解析
Agent 配置是一份独立副本，不包含发起请求的 Session。
`session_bound=True` 表示在真实 Session 的材料就绪之前，无法通过
`EngineAdapter.prepare_runtime` 初始化引擎。

平台仍会准备镜像、工作区账户、Plugin/Skills 缓存、控制服务和共享席位。待领取库存不
包含任何个人 Session 材料，其回执记录 `engine_initialized=False`。领取这类库存时，
平台保留其放置位置，交付 Session 材料，然后激活引擎，但不会把它当作已经初始化的厂商
运行时。失败时只丢弃被领取的那一个单元，不会悄悄改为冷启动。

provider 名称和声明会计入运行时代次（runtime generation）；它们变化后，待领取库存随之
失效。`session_bound=False` 时，普通的已初始化预热和受保护的 Vault 凭证替换保持原有
路径。与 Session 绑定的资源预热不依赖 Credential Vault：它不把模型凭证和环境凭证放进
尚未领取的库存，而是在领取时解析。它不会实现不可用的 Vault 能力。

这只控制引擎适配器执行的初始化。所需的系统服务和包装程序（wrapper）必须已经构建进所选
镜像。provider 不能事后向沙箱加装包装程序，也不能推迟镜像自身的入口程序（entrypoint）。

## 同步已准备的资源

`async synchronize(*, context: ResourcePreparationContext) -> None` 在沙箱中实际生成
Plugin 和 Skills 缓存之后、任何引擎使用它们运行之前执行。`context.operation.subject`
说明这些资源属于谁：

- **Agent**（`kind="agent"`）：每个预先准备的沙箱执行一次，在它成为待领取库存之前。
  没有预热容量的 Agent 从不执行同步；领取、冷启动、重新接入和输入刷新都不会调用它。
- **Assistant**（`kind="assistant"`）：其工作区每次准备 Assistant 的一个修订版本时
  执行，包括工作区创建时，以及对话或平台在运行中的沙箱上准备编辑后的 Assistant 时，
  都在 Hermes 运行该修订版本之前。接入已为当前修订版本准备好的工作区的对话不会调用它。

按对话的声明应放在 `prepare_turn` 中。

| 上下文 | 含义 |
| --- | --- |
| `operation.subject` | `PreparationSubject(kind, id)`：Agent 或 Assistant 的 ID，即外部身份的键 |
| `operation.operation_id` | 持久的调用标识，用于幂等和恢复 |
| `owner_user_id` | 已保存的 Agent 创建者，或这个 Assistant 工作区所属的用户 |
| `revision` | Agent 的编写版本，或 Assistant 的配置修订版本 |
| `preparation_id`、`runtime_generation` | Agent 的池生命周期，或 Assistant 的沙箱；已解析的配方 |
| `config` | 已解析配置的独立副本 |
| `sandbox` | 标准 `SandboxHandle`，包括其文件接口 |
| `packages` | 按顺序排列的来源声明，以及实际的缓存检出路径 |
| `skills_path` | 实际准备好的 Skills 目录，或 `None` |

读取实际准备好的文件，并合并部署的声明。外部身份以 subject 为键，在不同修订版本和
沙箱之间保持不变。没有变化的外部写入由 provider 自行跳过。不要把受信任的配置写入日志。

平台已保存操作上的比较并设置（compare-and-set）使宿主发起的 `synchronize` 调用
在各副本和各沙箱之间串行进行。对 Agent，宿主在同步前后以及发布
库存之前检查版本、刷新修订号、池生命周期和各项代次；对 Assistant，宿主检查该修订版本
是否仍是当前版本。已被取代的结果会以 `AGENT_RUNTIME_GENERATION_CONFLICT` 或
`ASSISTANT_REVISION_CONFLICT` 拒绝。同一 subject 的另一次同步正在运行时，Agent 的准备
以 `RESOURCE_SYNC_BUSY` 失败并重试；Assistant 的准备则等待它完成。

异常、超时、取消或宿主丢失会留下一个未决操作（先是 `RESOURCE_SYNC_FAILED`，然后是
`RESOURCE_SYNC_UNRESOLVED`）。
它的截止时间到期后允许调用 `async recover(*, operation: ResourcePreparationOperation)`，
但不允许再发起结果不确定的新写入。只有在确定这次操作（包括延迟到达的请求）再也不可能
写入之后，才返回 `True`。恢复必须幂等，且不得阻断（fence）较新的操作。默认实现返回 `False`。
成功的 `synchronize` 不得留下尚未完成的外部写入。仅凭租约过期或远端没有返回结果，
不能证明已经没有写入在进行。

### 提供方状态与事务边界

上下文是宿主提供的可信快照，不是持续持有的数据库事务或 subject 锁。所有者、修订和操作
标识本次准备，不保证 provider 运行期间这些值仍是当前值。Assistant 修订包含继承的默认
配置与 Environment 配置，不只是自身字段。宿主会拒绝过时结果，但不能撤销已经提交的
provider 写入或已经生效的远程请求。

外部绑定与操作日志保存在 provider 自己的存储中。以 `(subject.kind, subject.id)` 为绑定键，
使修订或替换沙箱后仍保留相同外部身份；用 `operation_id` 标识调用的持久工作与恢复记录。
provider 自己的注册端点也应协调这些记录，用唯一性与条件更新防止竞争注册和过时写入。
宿主操作协调不会串行化该端点独立发起的写入。

provider 可以通过公开的 `PreparationCommitGuard`，在提交自己的绑定与日志时，同时校验
subject 的权威状态。宿主发起的 `synchronize` 提供 `context.commit_guard`，绑定真实操作
和准备代次。必须在 provider 表写入的**同一连接、同一事务**中校验；写入前后分别读取
平台状态不提供相同保证。不要用平台负责的定义字段存放 provider 私有状态。

Assistant 首次同步发生在工作区发布 `READY` 之前。guard 在持有相应数据库锁时校验
所属 materializer Session、启动所有者、沙箱代次和记录的分配。已就绪工作区的同步校验
已发布沙箱。两条路径都保留操作、所有者、修订和截止时间检查。

注册等独立 provider 状态写入，从 `astrabox.runtime_preparation` 导入
`issue_preparation_commit_guard`。在 AstraBox 身份中间件内调用，或由已绑定经身份解析器
验证身份的嵌入应用调用；绝不能绑定请求 JSON 给出的用户 ID。工厂要求明确绑定身份，
以及 Agent 管理权限或 Assistant 所有权；机器身份还需 `astrabox:write` 或 `astrabox:admin`。
没有绑定上下文时，默认本地身份查询不足以授权。签发读取权威所有者与修订，返回后不持锁，
不创建准备操作，并拒绝正在同步的 subject。写入事务内再次校验权限和当前没有操作的条件。

```python
from astrabox.persistence.provider import get_persistence
from astrabox.runtime_preparation import issue_preparation_commit_guard
from astrabox.seams import PreparationSubject

# Inside an authenticated provider handler; no remote call in this block.
guard = await issue_preparation_commit_guard(
    subject=PreparationSubject(kind="agent", id=agent_id),
)
async with get_persistence().get_engine().begin() as connection:
    await guard.validate(connection)
    # provider_write is a SQLAlchemy statement for the provider's own table.
    # Its constraints and operation journal remain the provider's responsibility.
    await connection.execute(provider_write)
```

使用所选持久化引擎的实际 `AsyncConnection`，以及 `READ COMMITTED` 隔离的活动根事务。
其他引擎、自动提交与保存点会被拒绝。在锁定 provider 自有行之前校验；每事务只操作一个
subject，拒绝必须向上传播以回滚，事务保持短暂。定义、Environment、默认配置和操作锁
持续到提交或回滚。Agent 必须仍为 `ACTIVE`；Assistant 已删除、所有者/修订过时，或
回调操作过时/过期时，返回 `PREPARATION_SUBJECT_CONFLICT`。

Assistant 按实际继承配置校验修订，包括默认配置的首次插入。Agent 的 `revision` 是自身
定义的 `version`，仅修改其 Environment 不会增加该修订。Agent 回调还校验记录的准备代次，
但不重新计算有效 Environment 修订。Environment 锁阻止已校验事务中的并发编辑，不证明
从签发 guard 或捕获回调配置以来 Environment 一直未变。停用的 Environment 仍允许校验；
能否启动由启动路径负责。Environment 编辑会改变 Assistant 有效修订，即使所有者相同，
此前签发的 guard 也已过时。独立 guard 要求当前没有活动操作，即使同步是在签发后开始。
一次校验成功，不保证之后的其他事务。

远程调用放在数据库事务之外。发起远程写入前记录持久意图，并使用远端支持的幂等与隔离
机制。独立 provider 数据库不能共享 AstraBox 本地事务，guard 对其没有保证。
两种方式都不能使远程效果与数据库提交原子化，也不保证远程恰好执行一次。
不确定结果保持未决，直到 `recover` 能证明原调用及延迟请求再也不能写入；
日志或租约过期本身不是证据。无法证明时返回 `False`，不能放行新的不确定写入。

## 交付 Session 材料

`async prepare_session(*, context: SessionPreparationContext) -> RuntimeMaterials`
在冷启动、领取、重新接入和替换沙箱时，于引擎激活之前运行。其中的 `principal_id` 来自
已保存的 Session 所有者。上下文包括 Session ID、它的 `subject` 和 `revision`、已解析的
配置、运行时代次、沙箱 ID 和工作负载身份。对于 Assistant 对话，它在 Assistant 的资源
同步之后运行。在没有对话的情况下准备 Agent 的库存或 Assistant 的工作区时，不提供任何
Session。

返回若干 `RuntimeFile(name, content: bytes)` 条目和一个环境变量映射。
环境变量的值需要指向某个返回的文件时，使用 `context.file_path(name)`。
平台把每个文件写入工作负载的私有主目录，归该工作负载所有，权限为 `0600`，位于任务
工作区之外。文件名必须是安全的纯文件名（basename）；对同一个 Session 和沙箱，路径
保持不变。材料字节和环境变量值不会出现在对应 dataclass 的表示形式和公开错误中。与
Session 绑定的 provider 必须返回材料。

环境变量在它所启动的运行时的整个生命周期内保持不变。引擎适配器必须声明
`supports_startup_environment()`，并在原生初始化之前交付
`EngineStartupContext.runtime_env`。内置 Claude Code 适配器通过其 SDK 选项完成这一步；
其他内置适配器会拒绝 Session 环境变量，而不是忽略它。由平台负责的环境变量名不能被覆盖。
拒绝会以 `RUNTIME_PREPARATION_FAILED` 阻止激活；异常文本不会公开。

Claude Agent SDK 在启动 CLI 之前会先用 `claude -v` 探测它，此时不带选项中的环境变量。
如果镜像中的包装程序要求有 Session 材料才放行 CLI，应原样透传 `-v` 和 `--version`。

## 准备每次引擎写入

`async prepare_turn(*, context: TurnPreparationContext) -> RuntimeMaterials` 在每条
输入交给引擎之前立即运行。调用它的是 Session 的读取方（reader），也就是持有该
Session 与其运行时之间连接的那个进程，因此同一个 Session 的输入按接收顺序逐条准备。
读取方会先重新加载 Session。`context.turn_id` 和 `context.dispatch_attempt_id` 都
携带这条输入的 id。如果接收输入的运行时在接纳这条输入之前被替换，这条输入会被再次交出，
并以相同的 id 再次准备，因此钩子对它必须是幂等的。只返回更新过的文件；平台把它们写到
`prepare_session` 使用的同一路径，因此 `context.file_path(name)` 指向同一个文件。返回
非空的环境变量会使这条输入失败。provider 自己负责的状态也可以在这里通过
`context.sandbox_handle` 更新。

钩子的截止时间为 30 秒。超时或异常会以 `TURN_PREPARATION_FAILED` 拒绝这条输入：
输入不会到达引擎，会被记录为失败，Session 的事件流以 `data-input-failed` part 报告它。
每次交出输入时，平台调用钩子一次；引擎处理这条输入期间不会再次调用。由于钩子在每条
输入之前都会运行，凭证在有效期内时应直接复用并立即返回；只在凭证即将过期时才调用部署的
服务。按输入的准备（`prepare_turn`）需要声明了 `sandbox_is_profile_exclusive` 的沙箱后端。

## 要求用户操作

用户必须先完成某项操作时，从 `prepare_session` 或 `prepare_turn` 抛出
`PreparationUserActionRequired(message=..., url=...)`。宿主会校验其中的消息、状态和
可选的 HTTP(S) URL；其他任何异常都只报告通用错误，因为 provider 的文本可能带有机密
信息。从 `prepare_turn` 抛出时，它以 `TURN_PREPARATION_USER_ACTION_REQUIRED` 拒绝
这条输入：输入连同经过校验的操作被记录为失败，Session 的事件流以携带 `user_action`
的 `data-input-failed` part 报告它。不会自动重新发送任何内容。

对话启动时抛出的用户操作要求不会结束对话。对话保持打开（`READY`，运行时不可用），
平台记录这项操作。平台为一条消息再次启动运行时之前，会先读取 `read_user_actions`。
只要它报告该要求处于待处理状态，运行时就不会启动，也不会占用沙箱：消息以
`TURN_PREPARATION_USER_ACTION_REQUIRED` 和该操作失败，Session 的事件流以携带
`user_action` 的 `data-input-failed` part 报告它。它报告就绪后，下一条消息会启动运行
时，并在 `prepare_session` 成功后送达。该要求待处理期间，显式恢复
（`POST /api/v1/sessions/{session_id}/recover`）会立即以
`TURN_PREPARATION_USER_ACTION_REQUIRED`（409）和该操作被拒绝。Agent 对话的启动停止时，
其沙箱会被释放，下次启动时再重新获取沙箱。Assistant 的工作区则保持运行。

`read_user_actions(context=...)` 返回 `SessionUserActionState`，控制台通过
`GET /api/v1/sessions/{session_id}/user-action` 读取它。
`perform_user_action(context=..., action_id=...)` 通过同一路径的 `POST`，执行用户从
状态的 `actions` 中选择的操作。宿主会检查这项操作当前是否在提供之列；对于当前待处理的
要求，操作在各副本之间必须是幂等的。上下文携带已保存的 Session ID，以及该 Session 经过认证的所有者和
`subject`。读取需要读权限范围（read scope），执行操作需要写权限范围（write scope）。
provider 出错时，请求以 `SESSION_USER_ACTION_FAILED` 失败，而不会视为就绪；前置条件
未知或待处理时，控制台会阻止发送。状态读取只供参考：对所有调用方（包括不使用控制台的
调用方），最终都以 `prepare_turn` 为准。

HTTP 视图还携带平台负责的 `applicable`。对已结束 Session，或 Agent 为 `DELETING` /
`DELETED` 的 Session，GET 不调用 provider，返回
`{"applicable": false, "ready": false, "user_action": null, "actions": []}`。
POST 在读取或执行 provider 操作前，以 `409 SESSION_READ_ONLY` 拒绝；所有者与 API scope
检查仍生效。控制台知道对话只读时，不显示或查询前置条件；之前可执行状态发出的迟到响应
不能恢复操作。可执行对话返回 `applicable: true`，保留 provider 的就绪和失败语义。

启动或恢复改变 Session 绑定、页面重新获得焦点或可见、浏览器重新联网时，控制台重新读取
前置条件。返回页面的读取使用共享节流和进行中请求去重，没有前置条件轮询定时器。
过时操作被 `409 INVALID_REQUEST` 拒绝后触发状态读取，不重放操作。
这些读取不会发起授权同意或注册。

```python
from astrabox.seams import SessionUserActionCommand, SessionUserActionState

pending = SessionUserActionState(
    ready=False,
    user_action={"status": "pending", "message": "Complete the requested consent.",
                 "url": "https://identity.example/consent"},
    actions=(SessionUserActionCommand("check-current-consent", "Check consent"),),
)
ready = SessionUserActionState(ready=True)
```

不要在任何这些回调中分派输入、发起授权同意、重新注册 Agent 或启动后台写入。从部署
已有的凭证系统返回材料。取消时不得留下尚未完成的 provider 写入；已确认的平台文件投递
会在调用退出之前完成。

## 验证范围

`tests/e2e/runtime_preparation_test.py` 在真实部署上运行每个回调，使用一个测试夹具
provider，以及一个构建进镜像、挡在真实 Claude CLI 前面的门控包装程序（gate）。
它标记为 `deployment_shape`，因此常规的 live 测试通道（live lanes）不会运行它；请针对用该夹具
构建的部署运行它。其中的令牌文档是合成的。这不代表对任何特定凭证服务、镜像或沙箱后端
的验收。
