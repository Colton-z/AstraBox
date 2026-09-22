# 系统架构

AstraBox 将管理 Agent 的服务与运行 Agent 程序的沙箱分开。即使浏览器或开发者的电脑断开连接，Agent 也可以继续工作；沙箱中的程序则无法直接访问 AstraBox 账户和平台数据。

## AstraBox 如何工作

![AstraBox 各组件运行在哪里](./img/architecture-system.svg#inline)

| 组成部分 | 负责内容 |
| --- | --- |
| **AstraBox 服务** | 提供网页控制台和 API，完成用户认证与资源鉴权，保存 Agent 和 Session 记录，并向客户端实时返回工作进度。 |
| **OpenSandbox** | 在 Docker 或 Kubernetes 上创建、连接、暂停、恢复和删除隔离沙箱。 |
| **沙箱** | 包含 Agent 程序及其可用的系统软件。Environment 决定沙箱镜像、网络访问和生命周期。 |
| **Agent 程序** | 理解任务，调用模型和可用能力，持续工作直至需要用户输入或完成任务。 |

AstraBox 统一处理鉴权、运行环境分配、凭证、状态保存和输入输出投递。沙箱分配、隔离和
存储使用所选 Provider 支持的操作；Agent 程序通过适配器运行自己的工具、权限机制和
会话协议。这些集成都进入同一套平台工作流程。

在内置镜像中，Agent 程序读写 Session 的 `/workspace` 工作区。Agent 在沙箱中启动应用后，可以通过 OpenSandbox 提供的地址在浏览器中打开；这部分流量不经过 AstraBox API。

模型端点可以使用内置 LiteLLM、已有 LiteLLM 部署，或模型 Plugin 提供的端点。
Environment 表单列出当前部署已安装的 Agent 程序，并校验所选沙箱镜像。发布版本支持的
Agent 程序和镜像名称统一列在仓库介绍中。

## 主要组件

### Web 控制台与 HTTP API

React 控制台与其他客户端使用同一个 FastAPI 服务。生产镜像在容器内通过 `8000` 端口
提供这两个入口。服务负责：

- Agent、Environment、Session、Deployment、Assistant 和管理 API；
- 身份认证和资源鉴权；
- 已保存的 Session 历史和实时 SSE；
- 审批和问题响应；
- 通过所选 provider 管理沙箱生命周期；
- 绑定目标地址的 MCP 出站凭证和消息平台集成。

`GET /healthz` 用于存活检查；`GET /readyz` 表示当前副本是否应继续接收流量。滚动更新
设置见[部署 AstraBox](deploy.md)。

### 沙箱 provider

沙箱 provider 接口使用 AstraBox 自己的请求类型传递生命周期、网络、权限和受保护凭证，
不暴露 provider SDK。所选 provider 会转换自己支持的能力，并在任务开始前拒绝其他请求。

内置 `open_sandbox` Plugin 调用 OpenSandbox Lifecycle 服务。服务镜像可以让它使用本机
Docker，也可以连接到以 Kubernetes 为后端、单独维护的 OpenSandbox 部署。

OpenSandbox 创建沙箱、解析访问地址、续租，并执行部署支持的暂停、恢复和清理操作。
隔离强度由部署选择的容器运行时决定，例如 Docker、gVisor 或 Kata。详见
[OpenSandbox provider](providers/opensandbox.md)。

对于沙箱内的 HTTP 服务，AstraBox 先完成访问鉴权，再返回 OpenSandbox 地址。浏览器随后
通过 OpenSandbox 的 Docker 端口转发或 Kubernetes ingress 连接；应用流量无需经过
AstraBox，仍可使用相对路径资源、SSE 和 WebSocket。

### Agent 程序

每个已安装的 Agent 程序都有一个 adapter，将它的原生 API 接入 AstraBox Session 操作。
推理、工具选择、权限含义和程序专属配置仍由该 Agent 程序定义。

适配器声明镜像、启动输入、状态格式和连接要求。平台的公共启动流程准备工作区、解析
凭证、恢复已保存的原生状态，然后激活适配器。独占沙箱和 Agent 共享沙箱中的隔离会话
使用同一套编排流程。

Environment 选择的镜像必须与 `engine_kind` 兼容。需要新增 Agent 程序时，按照
[接入新的 Agent 程序](writing-an-engine-adapter.md)实现所需接口和检查。

### 消息平台

服务镜像为当前支持的 Satori 官方 adapter 提供内置网关。AstraBox 保存 provider 配置、
只写 bot 凭证、入站消息、投递状态和来源游标；网关维持第三方连接并转换 provider Event。

具体消息平台无需另外部署 Koishi 或 Satori。Satori Protocol Server 选项用于连接部署方
已经运行的服务。多副本安装可以配置一个经过认证的外部消息网关。

### 数据与凭证

AstraBox 记录默认保存在 PostgreSQL。通过 `mongo` extra 可以使用 MongoDB；SQLite 适合
本地开发和测试。团队部署应使用共享 PostgreSQL 或 MongoDB。内置 LiteLLM 使用独立的
数据库和数据库角色。

AstraBox Credential Vault 是组织级资源。`local` Secret Store 使用 AES-GCM 加密，
`aws_kms` 使用 AWS KMS envelope encryption。管理员把 Vault 分配给 Agent 或 Assistant，
对话请求使用这项管理员分配。

用户身份与运行时凭证相互独立：用户身份决定谁能使用资源。受保护凭证传递也是独立步骤；
启用后，Agent 进程只收到占位符，所选沙箱 provider 只会在请求符合规则时加入真实凭证。
内置 OpenSandbox provider 通过它的出站 Credential Vault 完成映射。

平台先合并模型、MCP 和已配置的环境凭证，再交给所选沙箱 Provider。首次启动、从预热
资源分配和重新连接使用相同的凭证规则。Agent 程序适配器声明所需输入，Provider 实现
自身支持的传递方式。

## Session 运行时会发生什么

![一次 Session 请求的经过路径](./img/architecture-request-path.svg#inline)

1. 控制台、API 或集成向 Session 发送输入；
2. AstraBox 认证调用方、检查鉴权，并在开始执行前保存输入；
3. AstraBox 使用 Session 当前的沙箱。首次准备或重建运行环境时，AstraBox 读取当前 Agent 和 Environment，再请求 OpenSandbox 创建、分配、恢复或重新连接沙箱；
4. AstraBox 确认工作区和凭证，按需恢复原生状态，连接到所选 Agent 程序后再发送输入；
5. Agent 程序在 `/workspace` 中完成任务，并返回自身的原生输出；
6. AstraBox 保存 Session Event，并通过 SSE 实时发送给客户端；
7. 最终结果、问题、审批请求、中断或错误会更新 Session 状态。

关闭浏览器不会取消任务。客户端可以携带最后收到的 `after_seq` 游标重新连接，继续接收已经保存的 Event。详见 [SSE Event Stream](events-stream.md)。

运行环境可以通过以下路径启动：

| 路径 | 接收用户输入前的准备 |
| --- | --- |
| 首次启动 | 分配沙箱或隔离会话，准备工作区和凭证，再建立 Agent 程序连接。 |
| 预热启动 | 分配已准备的资源并关联到 Session，选择工作区，完成凭证交接和状态恢复后再激活。 |
| 重新连接或恢复 | 连接仍在运行的计算资源，或将已保存的原生状态恢复到替代资源，再调用程序的原生恢复操作。 |

预热资源可能早于 Session 创建，因此持久工作区在交付前选定。共享沙箱保留已有的根目录，
每个会话使用自己的隔离目录。如果确认计算资源已经丢失，受影响的任务会失败；后续消息
可以从已保存的原生状态继续，不会自动重新执行失败任务的输入。

## 数据保存在哪里

| 数据 | 保存位置 |
| --- | --- |
| Agent、Environment 和 Deployment 记录 | AstraBox 配置的数据库 |
| 凭证值 | 已配置的 Secret Store |
| Session 状态、消息和 Event | AstraBox 配置的数据库 |
| Agent 程序原生状态（SessionStore） | AstraBox 数据库，以程序原生记录或快照格式保存 |
| Session 和 Assistant 文件 | `/workspace`，可使用持久工作区卷；未配置时随沙箱或受支持的快照保留 |

原生状态包含 Agent 程序恢复会话所需的记录，也包括原生子会话状态。AstraBox 保存这些
记录，界面展示的消息历史不替代原生状态。替代运行环境恢复会话前，平台通过程序的状态
接口或原生文件还原记录。这项数据库存储不依赖持久工作区卷。

工作区文件有独立的保存方式。配置持久工作区卷后，替代计算资源访问同一个工作区目录。
未配置时，删除沙箱会移除其中的文件，除非已保留受支持的快照、下载文件、提交到代码
仓库或另行保存。Assistant 暂停会先保存原生状态，再释放计算资源；保留工作区文件还需
配置持久工作区存储。详见[文件](files.md)和 [OpenSandbox](providers/opensandbox.md)。

启用持久工作区时，存储插件提供底层文件系统，平台的公共 mergerfs 路由为每个沙箱提供
固定的目录入口。交付前，平台把入口关联到新建或已有工作区。OpenSandbox 只挂载工作区
子目录，不挂载路由的管理根目录。Agent 程序不负责选择存储介质或实现目录路由。本地
文件系统适用于单个沙箱主机；多台主机需要访问同一个共享文件系统。未配置工作区卷的
部署不需要运行目录路由。配置方法见[工作区部署](deploy.md#where-conversation-workspaces-live)。

`mounted_volume` 存储插件使用部署配置的卷。`aws_efs` 插件校验已经配置、支持共享访问的
EFS CSI Kubernetes 卷。两者使用同一套平台目录路由。部署者管理底层文件系统；使用 EFS
时，还需管理 EFS 文件系统、CSI 驱动和 AWS 凭证。

目录在沙箱交付给 Session 前选定。分配完成后，该入口不能改为另一个工作区。mergerfs
辅助进程运行在用户沙箱外，不提供任务执行期间切换文件系统的用户操作。在 Kubernetes
中，目录卷的节点亲和性使沙箱与其文件系统辅助进程运行在同一节点。

## 隔离与访问

Agent 程序不会收到 AstraBox 登录凭证，也不能直接访问 AstraBox 数据库。Environment 决定沙箱使用 `limited` 还是 `unrestricted` 网络模式。启用凭证保护后，受保护的模型、MCP 及已分配的出站 Vault 凭证保存在沙箱外，只会添加到符合规则的出站请求中。
这不代表所有环境变量都受同样保护：原生 tracing 鉴权会传入沙箱内 Agent 程序的
OpenTelemetry 配置，不经过出站 Vault 注入。参见 [tracing 配置说明](cli/configuration.md#tracing)。

沙箱启动前，镜像中已经包含 Agent 程序、沙箱内的 AstraBox 服务、系统账户和平台所需能力。预热沙箱可能早于使用它的 Session 创建，因此这些能力不能等到 Session 开始时再安装。通用软件和平台能力应在构建镜像时加入；只在分配沙箱后准备当前 Session 的工作区内容。

## 运行方式

| 方式 | Agent 任务运行在哪里 |
| --- | --- |
| 单台 Docker 主机 | OpenSandbox 在 AstraBox 主机上为沙箱创建独立容器。 |
| Kubernetes | OpenSandbox 在已配置的集群中创建沙箱工作负载。 |
| 已有 OpenSandbox 服务 | AstraBox 使用该服务所管理的 Docker 或 Kubernetes 运行环境。 |

三种方式使用相同的 AstraBox API 和 Session 行为。安装、安全和备份要求见[部署 AstraBox](deploy.md)。

任务如何分布取决于两个独立选择：

| 选择 | 共享内容 |
| --- | --- |
| AstraBox 后端副本 | API 和编排副本使用相同的产品数据库、凭证配置和可访问的沙箱服务。已保存的 Session 与原生状态不依赖某个副本的本机内存。 |
| 沙箱工作节点 | 沙箱服务在工作节点上分配计算资源。跨节点使用持久工作区需要共享底层存储，每个 mergerfs 目录视图与使用它的沙箱位于同一节点。 |

增加后端副本不会把节点本地文件系统变成共享存储。分别配置后端副本的数据库与服务访问，
以及沙箱工作节点的计算资源与工作区存储。

## 扩展接口 {#plugin-interfaces}

安装 Provider 包后，可以接入其他 Agent 程序、沙箱后端、模型服务、数据存储、Secret Store、身份提供方、工作区存储、扩展来源或消息平台，不需要改变 Session API。AstraBox 会在启动时检查 Provider 兼容性，并拒绝未知或不兼容的配置。

扩展方式见[嵌入 AstraBox](embedding.md)、[接入新的 Agent 程序](writing-an-engine-adapter.md)和[接入新的消息平台](writing-a-channel-provider.md)。

## 相关文档

- [概览](overview.md) — Agent、Environment、Session 和 Event
- [部署 AstraBox](deploy.md) — 安装与运维
- [OpenSandbox](providers/opensandbox.md) — 沙箱行为与配置
- [HTTP API](api.md) — 请求、认证、响应和流式输出
