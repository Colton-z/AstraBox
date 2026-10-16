# 接入新的 Agent 程序

Agent 程序是执行 agent loop 的软件，例如编程 Agent 的 CLI 或服务。要让新的 Agent 程序在 AstraBox 中运行，需要用适配器连接它支持的协议，并提供包含该程序的沙箱镜像。

AstraBox 将 Agent 程序运行成 7×24 小时在线的云端 Agent。Agent 程序保留自己的 agent loop、工具、事件名称、权限模式和对话状态；AstraBox 负责沙箱生命周期、消息保管、流式输出、已保存的 Session 记录，以及 API、网页控制台和渠道接入。

## 适配器

适配器将 Agent 程序支持的协议接入 AstraBox。它由两部分组成：

- **适配器类**声明程序在沙箱中需要什么，并在 AstraBox 准备好的沙箱中启动或重新连接程序；
- **运行时客户端**是到一段正在运行的对话的一条实时连接。它通过 AstraBox 对所有 Agent 程序通用的同一套接口约定，把输入交给程序、读取程序的事件、报告程序的状态，并转交回答和中断。

对话由程序的运行时负责。程序是否正在工作、取走了哪些输入、每条回复从哪里开始和结束、正在等待哪些审批，都以运行时客户端的报告为准；AstraBox 记录这些报告，从不自行推断。

代码接口名为 `EngineAdapter`，系统使用 `engine_kind` 这个存储字段选择已安装的适配器。这两个名称属于 Python 扩展 API；产品概念仍是 Agent 程序。

## 沙箱镜像

沙箱镜像包含 Agent 程序、操作系统依赖、控制服务，以及对话开始前所需的全部运行依赖。预热沙箱可能在 Session 领取它之前就已创建，因此 Agent 程序必须已经安装在镜像中，常驻服务也必须在沙箱报告就绪时处于运行状态。工作区内容和凭证不应写入镜像，而是通过 AstraBox 的沙箱生命周期提供。

## 适配器与沙箱镜像对比

| 维度 | 适配器 | 沙箱镜像 |
| --- | --- | --- |
| 用途 | 将 Agent 程序的协议接入 AstraBox | 提供 Agent 程序的运行环境 |
| 负责内容 | 协议请求、原生事件、对话标识、恢复游标和重新连接 | 可执行程序、系统依赖、运行账户、启动命令和常驻服务 |
| 何时修改 | Agent 程序的协议或支持能力发生变化 | 运行依赖或操作系统要求发生变化 |
| 加载方式 | Python 入口点 `astrabox.providers.engine` | Environment 中配置的运行镜像 |
| 验证方式 | 适配器和运行时客户端一致性测试，以及真实对话 | 在真实沙箱中启动并完成对话 |

接入新的 Agent 程序时，两部分都需要完成。只有适配器，无法启动 Agent 程序；只有镜像，也无法接入 AstraBox 的对话生命周期。

## 实现适配器

### 从已安装的接口开始

以目标 AstraBox 版本中安装的接口为准：

- `astrabox/core/service/orchestrator/engine/base.py` 定义 `EngineAdapter`、`EngineStartupContext`、`EngineStartupMaterialRequest` 和可选协议；
- `astrabox/core/service/orchestrator/engine/runtime.py` 定义运行时接口约定：`RuntimeClient`、`RuntimeInput`、`RuntimeCursor`、`RuntimeState`、各类运行时事件，以及 `initialize_runtime_client`；
- `astrabox/core/service/orchestrator/engine/capabilities.py` 定义 `EngineRuntimeCapabilities`，`engine/extensions.py` 定义 `EngineExtensions`；
- `astrabox/core/service/orchestrator/engine/provisioning.py` 定义 `EngineSandboxRequest`；
- `astrabox/core/service/orchestrator/runtime/models.py` 定义 `SessionRuntime`，即 `activate_runtime()` 的返回值；
- `astrabox/testing/engine_conformance.py` 定义共用的一致性检查；
- Agent 程序的官方协议类型和文档，定义其事件与选项的含义。

五个内置运行时客户端都是完整的示例。每个模块的 docstring 都说明了它的游标、回复边界，以及哪条厂商记录让一条输入算作已接纳：`engine/claude_runtime_client.py`、`engine/codex_runtime_client.py`、`engine/deepseek_harness_runtime_client.py`、`engine/hermes_runtime_client.py` 和 `engine/pi_runtime_client.py`。

Agent 程序已有的名称和值应保持不变。例如，它的 API 将某项设置称为 `approval_policy`，适配器就继续公开并传回 `approval_policy`，不要再换成 AstraBox 自己的近义词。

### 声明运行时

适配器通过 `capabilities` 属性返回 `EngineRuntimeCapabilities`。声明包括：

- 稳定的 `engine_kind`；
- `supported_session_kinds`，即它驱动的 AstraBox Session 类型（`agent_chat`、`assistant_chat`）；
- 引擎的 workload 信息（`EngineWorkloadDeclaration`），包括配置目录名称、指向该目录的环境变量，以及引擎自身执行路径在镜像中需要的命令；
- 对话的放置方式：引擎进程以对话自己的账户运行时为 `per_conversation_account`，集成仍驱动单个沙箱级服务时为 `box_account`；
- `default_runtime_image`，供 Environment 省略镜像时使用；
- `permission_modes` 和 `permission_mode_defaults`，使用引擎自己的名称；
- `extensions`（`EngineExtensions`）：它接受的 Agent 作者 MCP 传输方式、它发现 Skill 的位置、它的原生包类型、它的原生配置文档（对象块，其中 `protected_keys` 列出由平台管理的顶层字段），以及它是否把遥测数据发送到 Environment 的链路追踪端点；
- 当 AstraBox 需要在可替换沙箱外保存不透明的对话文件时使用的 `session_log`；
- `requires_process_sessions`：在对话独占的沙箱里，引擎作为沙箱的长驻进程运行、通过其 stdin 和 stdout 驱动（`SandboxHandle.processes`）时声明；这种情况下，把该引擎与 conversation tenancy（每段对话使用独立沙箱）搭配的 Environment，在未声明 `supports_process_sessions` 的沙箱后端上会被拒绝。

系统会在注册时校验这些声明。保存 Agent 时，适配器的 `extensions` 不接受的已配置值会被拒绝，不会保存成不生效的设置。链路追踪在保存 Environment 时校验：启用时要求适配器声明支持。切换到不支持该能力的适配器时，可以保留有效但已停用的链路追踪配置。

通过 `engine_client_type` 声明运行时客户端类。注册时会先检查这个类是否实现了完整的运行时接口约定，之后引擎才会出现在产品的任何可选列表中。

### 在沙箱中放置对话

沙箱共用方式不属于上述声明。一个沙箱承载多少段对话由 Environment 选择；由此确定的运行身份（账户命名、home 位置和组装账户的命令）由平台按 `(sandbox_tenancy, session_kind)`，使用适用于所有引擎的同一组规则生成。共享方式要求沙箱支持隔离会话，且适配器声明 `per_conversation_account`。如果引擎需要在该隔离会话内运行服务，请实现 `shared_conversation_service_launch()`。它必须在后台启动镜像中已安装的服务；平台会等待声明的端口就绪，再激活运行时。

发布经过校验的文本文件时，使用 `runtime/sandbox_script_writer.py` 中的 `install_verified_text_script`，并传入上下文的 `runtime_identity` 和运行时可见的路径。无论冷启动还是预热放置，平台都会在写入前把工作区路径映射到实际存储位置。不要在适配器中转换工作区路径。身份为 `None` 表示明确写入沙箱文件系统；提供的身份不完整时，会在任何写入之前被拒绝。

## 实现运行时客户端

AstraBox 为每个 Session 运行一个读取方（reader）：只有一个进程持有该 Session 的读取租约，以及到其运行时的唯一连接。这个进程把输入、回答和中断交给运行时，读取运行时的事件并记录下来。任何副本都可以接收用户的请求；数据库通知会唤醒读取方，由它通过自己的连接转交请求。编写客户端时，假定同一时间只有一个读取方；新的读取方会打开新的客户端。

| 成员 | 平台的要求 |
| --- | --- |
| `engine_kind` | 适配器的 `engine_kind`。 |
| `is_live`（属性） | 到运行时的连接是否处于打开状态。 |
| `engine_session_key`（属性） | 厂商的对话 ID。替换后的运行时会恢复这段对话。 |
| `submit(input)` | 把一个 `RuntimeInput` 交给运行时；运行时收到后即返回。按 `input_id` 保持幂等。运行时拒绝这条输入时，抛出 `RuntimeInputRefused(code, message, status_code=)`。 |
| `events(after=cursor)` | 一个异步生成器，在连接存续期间产出游标之后的运行时事件（游标为 `None` 时从头开始）。无法从该游标恢复时，抛出 `RuntimeCursorExpired`。 |
| `state()` | 运行时此刻对自身状态的报告（`RuntimeState`）。 |
| `answer(interaction_id, response)` | 把用户的回答转交给一项未关闭的交互。运行时没有这项未关闭的交互时，抛出 `InteractionNotOpen`。 |
| `interrupt(cancel_queued=False)` | 停止进行中的回复；设置 `cancel_queued` 时，同时丢弃排在它后面的输入。 |
| `committed(cursor)` | AstraBox 已持久记录游标及之前的全部事件。只在读取方取得历史之前保留历史的运行时，可以释放游标之前的内容。 |
| `capabilities()` | 已连接运行时的 `EngineCapabilityManifest`：权限模式、输入内容类型和控制操作。 |
| `close()` | 关闭连接。运行时本身会继续运行。 |

另有两个可选成员，实现后才会使用：`set_permission_mode(mode)`，读取方在连接时以及 Session 所有者修改权限模式时，用 Session 保存的模式调用它；`stop_child_run(control_ref)`，按你在子 Agent 信息中携带的控制引用停止一个子 Agent。

### 报告运行时事件

事件是一个小而封闭的集合。每个事件都保留厂商自己的用语：

| 事件 | 报告时机 |
| --- | --- |
| `InputAdmitted(input_id)` | 运行时已持久保存这条输入：即使运行时进程重启，也仍会回答它。 |
| `ResponseStarted(response_id, origin, input_ids)` | 运行时开始了一条回复。回复针对已提交的输入时，`origin` 为 `input`；运行时自行发起时为 `engine`。 |
| `ResponseOutput(response_id, frame)` | 一个浏览器 part：从厂商输出转换而来的 AI SDK UI message chunk。只有不属于任何回复的输出（例如 child run 的状态）才使用值为 `None` 的 `response_id`。 |
| `InteractionOpened(response_id, interaction_id, contract)` | 一条回复暂停，等待用户处理。 |
| `InteractionClosed(interaction_id, reason)` | 运行时停止等待这项交互：`answered`、`withdrawn` 或 `expired`。 |
| `ResponseEnded(response_id, outcome, native_reason, usage, error)` | 运行时结束了这条回复。`outcome` 是你声明的、对厂商结束方式的解读（`completed`、`failed` 或 `cancelled`）；`native_reason` 是厂商自己对结束原因的用词，保持不变。 |
| `StatusChanged(status)` | 运行时自身的状态发生变化：`working`、`waiting` 或 `idle`。 |
| `RuntimeDiagnostic(event_type, raw)` | 供运维人员查看的厂商原始证据；不会发送到浏览器。 |

每项事实都从厂商自己的信号中读取。回复 ID 在回复的整个生命周期内必须保持稳定，换成新的读取方后也是如此：请根据厂商的 ID 推导。运行时自行开始的后台工作报告为 `working`，回复暂停等待用户时报告为 `waiting`。

### 接纳输入

`submit()` 必须按 `input_id` 保持幂等。厂商按消息 ID 去重时，使用输入 ID（或由它推导出的值）作为该 ID；厂商不去重时，再次发送输入前先检查厂商的历史、队列或你自己的提交记录。正在工作的运行时会把输入放进自己的队列；如何回答排队的输入由厂商决定。

只在厂商写下能在运行时进程重启后保留的记录时（例如写入厂商对话记录的用户消息），才报告 `InputAdmitted`。如果厂商仍可能丢弃这条输入，提交请求的应答就不算接纳。输入被接纳之前，AstraBox 会保存它，并在当前运行时丢失时把它交给替换后的运行时；接纳之后，AstraBox 不会再次发送它。对于新的读取方没有收到其 `InputAdmitted` 的输入，它可以通过 `RuntimeState.admitted_input_ids` 结束对这些输入的保管。

### 保证游标的确定性

每个事件都带有 `RuntimeCursor`，用于从该事件之后继续。`generation` 标明这个位置所属的运行时实例（一个 runner 进程、一个后端 epoch 或一个厂商会话）；`position` 是你自己的恢复状态，AstraBox 以 JSON 保存它，从不解读。AstraBox 把游标和已经转发的 part 一起记录；接管的读取方从这个游标继续。

再次读取 `events(after=cursor)` 时，必须按相同顺序产出该游标之后的相同事件。读取方会为它记录的浏览器 part 编号，因此实时看到某个 part、之后又从记录中读到它的跟随者，只会看到它一次。请基于厂商保留的内容（它的 journal、日志或历史 API）进行转换，并把该记录无法重建的转换状态放进 position。

游标来自另一个 generation，或者对应的位置厂商已经不再保留时，抛出 `RuntimeCursorExpired`。读取方随后会收回运行时已收到但从未接纳的输入，通过 `state()` 获取运行时自己的报告（未结束的回复、待处理交互、排队中和已接纳的输入，以及继续读取的游标），然后核对：运行时不再报告的回复以失败结束（`runtime_lost`），运行时不再等待的交互以过期关闭。

### 组织回复的浏览器 part

一条回复的 part 以 `start` part 开始，其 `messageId` 是回复 ID，`messageMetadata` 带有 `response_id`、`input_ids` 和 `origin`。回复以 `finish` part 结束；失败的回复会在 `finish` 之前发送一个 `error` part。

回复暂停等待交互时，先带着交互约定报告 `InteractionOpened`，再发送结束浏览器当前消息段的 part：工具审批对应的 `tool-approval-request` part（`build_tool_approval_request_frame`）、`data` 为交互记录的 `data-interaction` part，以及带有 `finishReason: "tool-calls"` 的 `finish` part。然后报告 `StatusChanged("waiting")`。这条回复的任何后续 part（无论是继续输出还是另一项交互请求）之前，都要用同一个 `messageId` 的 `start` part 重新打开同一条消息。使用 `engine/interaction_contract.py` 中的辅助函数构建交互约定，并用 `validate_interaction_contract` 检查：它声明用户可以怎样回答（表单、在声明的选项中做出决定，或工具审批），并原样携带厂商的标识和含义。

### 报告工具调用

工具调用以 UI 流上的三个 part 到达浏览器，控制台据此构建一张卡片：

```python
from astrabox.core.service.orchestrator.engine.runtime import ResponseOutput

yield ResponseOutput(cursor, response_id, {"type": "tool-input-start",
                     "toolCallId": call_id, "toolName": name})
yield ResponseOutput(cursor, response_id, {"type": "tool-input-available",
                     "toolCallId": call_id, "toolName": name, "input": arguments})
yield ResponseOutput(cursor, response_id, {"type": "tool-output-available",
                     "toolCallId": call_id, "output": result})
```

`toolName` 和 `input` 都是程序自己的值，原样传递：如果控制台必须按名称识别工具，它只能适用于某一个 Agent 程序，换成下一个程序就什么也显示不出来。

`ResponseOutput` 通过共用的 `public_ui_frame()` 辅助函数为工具输入 part 加上 `dynamic: True`，因此适配器不需要另外维护一份这条展示规则。浏览器中的 AI SDK 根据它决定 part 的类型：设置时构建 `dynamic-tool` part；省略时构建带类型的 `tool-<name>` part，而控制台中所有读取方都会忽略这种 part。输出 part 不带这个字段：SDK 按 `toolCallId` 把它匹配到输入 part 已经创建的 part 上。

对于已完成的原生文件改动，使用 `engine/file_changes.py` 中的 `file_changes_frame` 发出 `data-file-changes`。适配器提供工具调用标识、改动的路径，以及原生内容对、unified patch、结构化 hunk 或单独的片段。厂商确认已写入但没有提供对比内容时，传入 `diff=None`。不要根据请求中的工具输入构造已应用的 diff。AstraBox 把这个 part 与产生它的回复一起记录，Diff 面板从那里读取。

### 报告 child run

如果 Agent 程序把工作委派给子 Agent，请把每个子 Agent 的生命周期和消息报告为 `data-subagent` part，其数据遵循 `engine/child_runs.py`。为每个子 Agent 选定一个稳定的原生引用；AstraBox 据此生成公开的 child-run ID，并且不公开原生引用。不属于任何回复的子 Agent part，使用 `response_id=None` 报告。

可选能力就是可选的：没有子 Agent 的程序同样是有效的 Agent 程序。不要为了满足通用接口而虚构子 Agent、权限模式或后台执行。按实际声明的能力选择真实环境验收用例；不支持的能力报告为不适用，而不是报告为测试通过。已声明的能力必须通过用户可见的完整路径，包括该能力支持的流式输出和已保存历史读取。

失败的影响范围同样以程序原生行为为准。子 Agent 失败或查看请求失败，不能证明父程序已经停止。只在程序自己的信号到来时结束回复；本地请求失败作为诊断信息保留，不要虚构成功结果。

### 连接与重新连接

使用 Agent 程序支持的协议，例如 HTTP API、双向 RPC 接口、SDK，或有文档说明的无界面进程协议。该协议的请求和事件用语由适配器负责。

基于平台准备好的 `EngineStartupContext` 实现 `activate_runtime()`。对于新 Session，它启动或连接 Agent 程序；当 `context.attach_mode` 有值时，适配器必须使用平台提供的续接键和材料，恢复该 Session 原来那一个厂商原生对话。此时 AstraBox 已经重新接管沙箱，并刷新了工作区和凭证；厂商可以在恢复过程中重建进程及内存里的会话对象，但恢复后的客户端必须报告同一个原生对话键。不得把已保存的 Session 绑定到新的或空白的厂商对话。

返回之前，把客户端传给 `initialize_runtime_client(client, engine_kind=..., resume_session_key=context.resume_session_key)`。它检查客户端处于连接状态、恢复的是所请求的对话，并且声明了适配器注册的权限模式，然后返回能力清单。最后返回一个 `SessionRuntime`，其 `engine_client` 是该客户端，`engine_manifest` 是这份清单。

### 声明沙箱要求

从 `sandbox_request()` 返回 `EngineSandboxRequest`。请求包含镜像入口命令、模型凭证的接收方式、工作目录环境变量、其他环境变量、需要公开的端口，以及可选的就绪检查端口。

通过 `startup_material_request()` 返回 `EngineStartupMaterialRequest`，声明需要的平台
对话记录（transcript）存储、原生运行状态存储、沙箱死亡通知或具名平台密钥。AstraBox 解析密钥并签发限定
范围的回调凭证，再通过 `EngineStartupContext` 交给适配器；适配器使用这些材料，
不自行签发平台凭证。

只有 AstraBox 平台决定领取、创建、重连还是替换沙箱。平台负责应用所选后端、工作区和运行身份、模型与 MCP 凭证、网络策略及生命周期跟踪，然后把准备好的上下文交给 `activate_runtime()`。适配器只声明 Agent 程序需要什么，并完成厂商协议握手；它不调用平台的创建流程，也不决定沙箱分配。

## 构建沙箱镜像

### 运行时与操作系统

选择 Agent 程序支持的操作系统和 CPU 架构。按固定版本安装 Agent 程序，并使用其有文档说明的无人值守接口。不要依赖镜像中不存在的软件。

### 镜像中的工具

安装适配器运行配置中声明的每一条必需命令。可选的开发工具不应放进必需命令列表；缺少必需命令时，系统会在 Session 接收输入前终止启动。

### 工作目录

运行配置定义用户主目录、工作区、源码目录、文件根目录、缓存目录和临时目录。应使用系统解析后的路径，不要假定它们一定是 `/root`、`/home` 或某个固定 UID。

### 安装额外软件

在构建镜像时安装系统软件包、语言运行时、Agent 程序和常驻服务。不要在 Session 启动时安装系统能力：准备好的沙箱早在每个 Session 的输入出现前就已经存在。

### 资源与超时

可用 CPU、内存、磁盘和执行超时取决于沙箱后端和部署配置。请在目标后端验证 Agent 程序的最低要求。程序无法处理请求时，就绪检查服务必须明确地让启动失败。

### 文件持久化

同一个沙箱存续期间，本地文件会继续存在。可选的持久工作区可在沙箱替换后保留工作文件。
原生对话状态单独保存在 AstraBox 数据库中，不依赖工作区存储卷。

原生对话使用 JSONL 日志时，请声明 `session_log`。AstraBox 同步其中的 JSON 记录，
并在重新连接前恢复到替换后的沙箱中。记录内容和顺序会保留，但 JSONL 空白格式不保证
逐字节一致；平台不解释厂商语义。使用其他原生状态格式的引擎可以声明
`runtime_state_store`，并提供对应的保存和恢复实现。如果这份原生配置属于某个 Session
的隔离账户，请声明 `runtime_state_scope="session"`；默认值 `"workspace"` 把它绑定到
Agent 或 Assistant 的工作区。归属标识和访问凭据由平台签发。引擎原样转交这个归属标识，
并且只在所需的原生数据进入数据库保管之后才发布引用。DSH 为得到这项确认扩展了厂商自带的
本地附件后端，同时保留原生的规范化、不可变发布和经过校验的读取。它的恢复屏障会在
Session 于替换计算资源上打开之前运行。

### 执行用户与环境变量

在镜像中创建所有必需账户和可写目录。运行时可能以非 root 用户运行；Agent 程序报告就绪之前，系统解析出的工作区必须可写。凭证通过创建沙箱的接口传入，不要把凭证写进镜像，也不要输出到日志。

## 注册适配器

通过 Python 包的入口点发布适配器类：

```toml
[project.entry-points."astrabox.providers.engine"]
example = "example_package.adapter:ExampleAdapter"
```

入口点名称必须与 `ExampleAdapter.engine_kind` 一致。适配器类可以通过 `seams_api_version` 声明开发时使用的 AstraBox 扩展接口版本（当前为 `7`）；固定的版本不兼容时，AstraBox 会在启动时报告版本不匹配并失败。入口点重名、适配器不完整，以及运行时客户端没有实现完整的运行时接口约定，也会导致启动失败。

## 验证接入

按以下顺序完成检查：

1. 使用受支持 Agent 程序版本产生的协议记录编写事件转换测试，不要使用手写的 payload。
2. 检查游标的确定性：从任意游标重新读取已记录的事件，都会得到该游标之后相同的事件。
3. 运行 `astrabox.testing.engine_conformance` 中的 `EngineAdapterContractSuite` 和 `RuntimeClientContractSuite`。两者都只检查结构；驱动真实对话属于你的端到端测试：

   ```python
   from astrabox.testing.engine_conformance import (
       EngineAdapterContractSuite,
       RuntimeClientContractSuite,
   )

   class TestExampleAdapter(EngineAdapterContractSuite):
       def make_adapter(self):
           return ExampleAdapter()

   class TestExampleRuntimeClient(RuntimeClientContractSuite):
       def client_type(self):
           return ExampleRuntimeClient
   ```

4. 通过真实的 OpenSandbox 生命周期服务启动镜像，并在同一段对话中完成两次输入，其中一次在程序仍在工作时发送。
5. 覆盖中断，以及适配器声明的每一项可选能力，包括在读取方重新连接之后才回答的审批。
6. 在回复流式输出期间重启 AstraBox 服务，确认回复从已记录的游标继续，没有重复或缺失的 part。
7. 在回复运行期间删除沙箱，确认这条回复以失败结束，并且下一条消息会在新沙箱上恢复同一个原生对话。

固定的 Agent 程序版本更改协议或运行要求时，应同时更新协议记录、适配器和镜像。

## 相关文档

- [系统架构](./architecture.md) — AstraBox 的职责与扩展接口
- [OpenSandbox Provider](./providers/opensandbox.md) — 沙箱生命周期与运行行为
- [Environment](./environments.md) — 镜像、资源和网络访问
