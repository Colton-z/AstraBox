# 接入新的 Agent 程序

Agent 程序是执行 agent loop 的软件，例如编程 Agent 的 CLI 或服务。要让新的 Agent 程序在 AstraBox 中运行，需要用适配器连接它支持的协议，并提供包含该程序的沙箱镜像。

AstraBox 将 Agent 程序运行成 7×24 小时在线的云端 Agent。Agent 程序继续负责自己的 agent loop、工具、事件名称、权限模式和对话格式；AstraBox 负责沙箱生命周期、消息传递、流式输出、Session 状态，以及 API、网页控制台和渠道接入。

## 适配器

适配器将 Agent 程序支持的协议接入 AstraBox。它负责启动或重新连接 Agent 程序、传递输入、输出事件、停止正在执行的任务，并保留 Agent 程序原生的对话标识。

代码接口名为 `EngineAdapter`，系统使用 `engine_kind` 这个存储字段选择已安装的适配器。这两个名称属于 Python 扩展 API；产品概念仍是 Agent 程序。

## 沙箱镜像

沙箱镜像包含 Agent 程序、操作系统依赖、控制服务，以及对话开始前所需的全部运行依赖。预热沙箱可能在 Session 使用它之前就已创建，因此 Agent 程序必须已经安装在镜像中，常驻服务也必须在沙箱报告就绪时处于运行状态。工作区内容和凭证不应写入镜像，而是通过 AstraBox 的沙箱生命周期提供。

## 适配器与沙箱镜像对比

| 维度 | 适配器 | 沙箱镜像 |
| --- | --- | --- |
| 用途 | 将 Agent 程序的协议接入 AstraBox | 提供 Agent 程序的运行环境 |
| 负责内容 | 协议请求、原生事件、对话标识和重新连接 | 可执行程序、系统依赖、运行账户、启动命令和常驻服务 |
| 何时修改 | Agent 程序的协议或支持能力发生变化 | 运行依赖或操作系统要求发生变化 |
| 加载方式 | Python 入口点 `astrabox.providers.engine` | Environment 中配置的运行镜像 |
| 验证方式 | 适配器和客户端一致性测试 | 在真实沙箱中启动并完成对话 |

接入新的 Agent 程序时，两部分都需要完成。只有适配器，无法启动 Agent 程序；只有镜像，也无法接入 AstraBox 的对话生命周期。

## 实现适配器

### 从已安装的接口开始

以目标 AstraBox 版本中安装的接口为准：

- `astrabox/core/service/orchestrator/engine/base.py` 定义 `EngineAdapter`、`EngineClient` 和可选协议；
- `astrabox/core/service/orchestrator/engine/capabilities.py` 定义 `EngineRuntimeCapabilities`；
- `astrabox/core/service/orchestrator/engine/provisioning.py` 定义沙箱请求和创建结果；
- `astrabox/testing/engine_conformance.py` 定义共用的一致性检查；
- Agent 程序的官方协议类型和文档，定义其事件与选项的含义。

Agent 程序已有的名称和值应保持不变。例如，它的 API 将某项设置称为 `approval_policy`，适配器就继续公开并传回 `approval_policy`，不要再换成 AstraBox 自己的近义词。

### 声明运行要求

适配器通过 `EngineRuntimeCapabilities` 声明：

- 稳定的 `engine_kind`；
- 支持的 AstraBox Session 类型；
- Agent 程序的 workload 信息（`EngineWorkloadDeclaration`），包括配置目录名称、指向该
  目录的环境变量，以及镜像中需要的命令；
- 对话的进程位置：进程使用对话自己的账户时为 `per_conversation_account`，adapter
  仍驱动一个使用镜像账户的沙箱级服务时为 `box_account`；
- Environment 未指定镜像时使用的默认运行镜像；
- Agent 程序公开的权限模式及其默认值；
- 描述 Agent 程序专属设置的 `engine_options_schema`；
- 当 AstraBox 需要在可替换沙箱外保存原生对话文件时使用的 `session_log`；
- `configuration_inputs`，即适配器实际使用的 AstraBox 配置：MCP 服务、Skill、Plugin 或链路追踪。

系统会在注册时校验这些声明。保存 Agent 时，不属于该适配器 `configuration_inputs` 的已配置值会被拒绝，不会保存成不生效的设置。
链路追踪在保存 Environment 时校验：启用时要求适配器声明支持。切换到不支持该能力的
适配器时，可以保留有效但已停用的链路追踪配置。

### 实现客户端

沙箱共用方式不属于上述声明。一个沙箱承载多少段对话由 Environment 选择；对应的账户名、
home 路径和账户准备命令由平台按 `(sandbox_tenancy, session_kind)` 使用同一组规则为所有
Agent 程序生成。共享方式要求沙箱支持隔离 Session，且适配器声明
`per_conversation_account`。如果引擎需要在隔离 Session 内运行服务，请实现
`shared_conversation_service_launch()`，在后台启动镜像中已安装的服务。
平台会等待声明的端口就绪，再激活运行时。

`EngineClient` 对应一个原生对话。它必须：

1. 通过 `bind_conversation()` 将 AstraBox Session 绑定到准确的原生对话标识；
2. 通过 `deliver()` 幂等接收每一条持久化输入；
3. 在响应事件之前，为同一条输入输出 `data-input-consumed`；
4. 通过 `iter_turn_events()` 持续输出事件，直到 Agent 程序完成任务、请求用户输入或连接中断；
5. 取消或中断正在执行的任务；
6. 返回当前连接的 Agent 程序所支持的能力；
7. 关闭自己持有的连接和进程。

原生输出只在适配器内分类一次，分别成为公开界面事件、控制信息、结束结果或私有诊断。厂商事件名称、标识、结束原因和字段含义仍由适配器负责；核心调度代码不应推断这些含义。

如果 Agent 程序支持审批、提问、子任务控制、权限模式、服务信息、执行中重连或对话记录恢复，请实现 `base.py` 中对应的可选协议。只有客户端已经实现的能力才能对外声明。

### 连接与重新连接

使用 Agent 程序官方支持的协议，例如 HTTP API、双向 RPC 接口、SDK 或无界面进程协议。请求和事件的含义由 Agent 程序及适配器负责。

基于平台准备好的 `EngineStartupContext` 实现 `activate_runtime()`。新 Session 会通过它启动或连接 Agent 程序；当 `context.attach_mode` 有值时，适配器必须使用平台提供的续接键和材料，恢复该 Session 原来那一个厂商原生对话。此时 AstraBox 已经重新接管沙箱，并刷新了工作区和凭证；厂商可以在恢复过程中重建进程及内存里的会话对象，但恢复后的客户端必须报告同一个原生对话键。只有 Agent 程序能够继续未完成的流式输出时，才实现 `EngineLiveTurnReconnect`。否则，应明确结束中断的执行，并让下一条输入继续同一个原生对话；不得把已有 Session 静默绑定到新的或空白的厂商对话。

### 声明沙箱要求

从 `sandbox_request()` 返回 `EngineSandboxRequest`，描述 Agent 程序的运行要求。请求包含镜像入口命令、模型凭证的接收方式、工作目录环境变量、其他环境变量、需要公开的端口，以及可选的就绪检查端口。

通过 `startup_material_request()` 返回 `EngineStartupMaterialRequest`，声明需要的平台
对话存储、原生运行状态存储、沙箱死亡通知或具名平台密钥。AstraBox 解析密钥并签发限定
范围的回调凭证，再通过 `EngineStartupContext` 交给适配器；适配器使用这些材料，
不自行签发平台凭证。

只有 AstraBox 平台决定领取、创建、重连还是替换沙箱。平台负责应用所选后端、工作区和运行身份、模型与 MCP 凭证、网络策略及生命周期跟踪，然后把准备好的上下文交给 `activate_runtime()`。适配器只声明 Agent 程序需要什么，并完成厂商协议握手；它不调用平台的创建流程，也不决定沙箱分配。

## 构建沙箱镜像

### 运行时与操作系统

选择 Agent 程序支持的操作系统和 CPU 架构。固定 Agent 程序的版本，并使用其官方支持的无人值守接口。不要依赖镜像中不存在的软件。

### 镜像中的工具

安装适配器运行配置中声明的每一条必需命令。可选的开发工具不应放进必需命令列表；缺少必需命令时，系统会在 Session 接收输入前终止启动。

### 工作目录

运行配置定义用户主目录、工作区、源码目录、文件根目录、缓存目录和临时目录。应使用系统解析后的路径，不要假定它们一定是 `/root`、`/home` 或某个固定 UID。

### 安装额外软件

在构建镜像时安装系统软件包、语言运行时、Agent 程序和常驻服务。不要在 Session 启动时安装系统能力：准备好的沙箱早在每个 Session 的输入出现前就已经存在。

### 资源与超时

可用 CPU、内存、磁盘和执行超时取决于沙箱后端和部署配置。请在目标后端验证 Agent 程序的最低要求。常驻服务无法接收请求时，就绪检查应明确判定启动失败。

### 文件持久化

同一个沙箱存续期间，本地文件会继续存在。可选的持久工作区可在沙箱替换后保留工作文件。
原生对话状态单独保存在 AstraBox 数据库中，不依赖工作区存储卷。

原生对话使用 JSONL 日志时，请声明 `session_log`。AstraBox 同步其中的 JSON 记录，
并在重新连接前恢复到替换后的沙箱中。记录内容和顺序会保留，但 JSONL 空白格式不保证
逐字节一致；平台不解释厂商语义。使用其他原生状态格式的引擎可以声明
`runtime_state_store`，并提供对应的保存和恢复实现。

### 执行用户与环境变量

在镜像中创建所有必需账户和可写目录。运行时可以使用非 root 用户；Agent 程序报告就绪之前，系统解析出的工作区必须可写。凭证通过创建沙箱的协议传入，不要把凭证写进镜像，也不要输出到日志。

## 注册适配器

通过 Python 包的入口点发布适配器类：

```toml
[project.entry-points."astrabox.providers.engine"]
example = "example_package.adapter:ExampleAdapter"
```

入口点名称必须与 `ExampleAdapter.engine_kind` 一致。适配器类可以通过 `seams_api_version` 声明开发时使用的 AstraBox 扩展接口版本；版本不兼容时，AstraBox 会在启动时明确报告版本不匹配。入口点重名或接口不完整也会导致启动失败。

## 验证接入

按以下顺序完成检查：

1. 使用受支持 Agent 程序版本产生的协议记录，编写事件转换测试；
2. 覆盖输入传递、继续对话、流式输出、取消、重新连接，以及适配器声明的全部可选能力；
3. 运行 `astrabox.testing.engine_conformance` 中的 `EngineAdapterContractSuite` 和 `EngineClientContractSuite`；
4. 通过真实 OpenSandbox 生命周期服务启动镜像，并在同一个对话中完成两次输入；
5. 重启 AstraBox 服务，确认能够继续同一个原生对话，或者为未完成的执行返回明确的结束结果。

固定的 Agent 程序版本更改协议或运行要求时，应同时更新协议记录、适配器和镜像。

## 相关文档

- [系统架构](./architecture.md) — AstraBox 的职责与扩展接口
- [OpenSandbox Provider](./providers/opensandbox.md) — 沙箱生命周期与运行行为
- [Environment](./environments.md) — 镜像、资源和网络访问
