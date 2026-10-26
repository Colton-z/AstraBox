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
| `plan` | 每次解析允许接收任务的配置时异步调用，读取已持久化的主体策略 |
| `synchronize`、`recover` | 每个预先准备的 Agent 沙箱一次，在它成为待领取库存之前；对 Assistant，在其工作区每次准备一个修订版本时 |
| `prepare_session` | 引擎激活之前：冷启动、领取、重新接入、替换沙箱 |
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

## 在绑定 Session 用户后激活 MCP

在 `RuntimePreparationPlan.session_mcp_servers` 中声明需要最终用户身份的连接：

```python
from astrabox.seams.runtime_preparation import RuntimePreparationPlan, SessionMCPServer

async def plan(self, *, config):
    return RuntimePreparationPlan(session_mcp_servers=(
        SessionMCPServer(name="personal-service"),
        SessionMCPServer(name="account-tools", plugin="company-tools"),
    ))
```

`name` 是配置中的服务键；`plugin` 是 Plugin manifest 的名称。Claude 适配器核对已安装的
Plugin 并解析供应商规定的服务别名，provider 不需要拼接 `plugin:*`。声明不包含凭据，
参与 runtime generation；默认空列表保持普通 MCP 的行为。不支持此能力的引擎会明确拒绝声明。

Claude 用私有原生项目配置中的 `disabledMcpServers` 暂停这些连接，并在发布预热库存前
核对原生禁用状态。引擎仍已初始化，receipt 为 `engine_initialized=true`。领取时先核对
持久化 Session 用户和授权前置条件，再交付该用户的文件与环境，通过原生启用／重连操作
连接指定 MCP，核对 `connected` 后才放行输入。普通 MCP 不会因此一律重连，已经连接的
服务也保持连接。provider 必须在缺少授权时明确拒绝或抛出 `PreparationUserActionRequired`。

已有空闲会话完成授权并配置声明后，可调用公开的
`POST /api/v1/sessions/{session_id}/recover`。它重新执行同一材料与连接屏障，保留仍存活的
Claude 进程和历史，不重发先前输入或重复业务写入。连接失败会阻止输入，直到显式恢复成功。
工作中的会话拒绝此操作。运行中的 runner 镜像必须已支持相应原生控制；只更新主机端代码
不能为旧镜像内的进程添加能力。沙箱确实丢失时仍走正常的原生 SessionStore 恢复路径。

文件与环境只影响在连接时读取它们的消费者，不能改写供应商在进程启动时已经展开的配置。
这类集成应使用供应商支持的刷新机制；无法延迟的启动配置仍需声明整个引擎
`session_bound=True`。MCP 声明、环境交付与模型凭据保护相互独立。

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

subject 所在行上的比较并设置（compare-and-set）使外部更新在各副本和各沙箱之间
串行进行：即 Agent 的行，或 Assistant 工作区的行。对 Agent，宿主在同步前后以及发布
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

引擎适配器声明 `startup_environment_scope()`。`"process"` 在原生初始化之前交付
`EngineStartupContext.runtime_env`；Claude Code 冷启动通过 SDK 选项交付。
声明 `supports_initialized_environment()` 的适配器也能在初始化后交付环境。
Claude 使用原生 `apply_flag_settings` 的 `env` 覆盖，并通过 `get_settings`
核对实际映射，确认后才释放输入，领取时保留同一个预初始化 CLI 进程；重连使用同一契约。
镜像必须声明支持这两个控制请求，旧镜像会明确拒绝。之后启动的 hook 和工具继承新值；
已经运行的子进程及仅在启动时读取值的功能不会被追溯更新。依赖启动材料的插件或包装
程序仍须声明 `session_bound=True`；用户绑定的 MCP 保留激活时的原生重连屏障。
`"session"` 通过原生会话上下文交付 `EngineStartupContext.session_env`，不改变共享进程
环境。Hermes 镜像包含固定上游提交、保留 MIT 许可的扩展，在原生 `session.create` 和
`session.resume` 中绑定映射，再由原生上下文传到工具子进程。重连和换箱时重新交付；
没有会话的 Assistant 工作区准备不接收这些值。缺少扩展的旧镜像会明确拒绝 RPC 参数。
返回 `None` 的适配器拒绝 Session 环境变量。由平台负责的环境变量名不能被覆盖；
Hermes 也拒绝原生身份和凭据变量。没有原生环境更新能力的已初始化进程拒绝新的进程作用域环境。
平台准备阶段的拒绝会以 `RUNTIME_PREPARATION_FAILED` 阻止激活，回调异常文本不会公开。
原生会话环境校验以 `AGENT_RUNTIME_ERROR` 阻止 Hermes 连接，消息不包含传入的环境变量值。

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
