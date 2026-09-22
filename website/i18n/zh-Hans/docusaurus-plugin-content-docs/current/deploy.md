# 部署 AstraBox

AstraBox 是自托管软件。你可以在一台 Docker 主机上运行完整服务，也可以把 AstraBox 接入 Kubernetes 中的 OpenSandbox，或者使用组织已经运行的 OpenSandbox 服务。仓库维护的单机部署会启动 Web 控制台、API、数据存储、模型与消息网关以及本地沙箱服务；Agent 任务在相互隔离的沙箱中运行。

![AstraBox 部署方式](./img/deploy-topology.svg#inline)

## 选择 Agent 沙箱的运行位置

| 部署方式 | 适用场景 | 沙箱运行位置 |
|---|---|---|
| 单台 Docker 主机 | 评估、开发和小型可信团队 | 主机 Docker 守护进程创建的独立容器 |
| Kubernetes 与 OpenSandbox | 多节点、预热沙箱、快照和集群管理 | OpenSandbox 创建的 Sandbox Pod |
| 已有 OpenSandbox 服务 | 组织单独运行沙箱基础设施 | 该服务配置的容器运行时 |

仓库维护的本地部署不启用认证，并且只监听本机回环地址。需要从其他网络访问 AstraBox 时，请先配置[团队登录](team-login.md)、TLS 和可信入口。

Kubernetes 和已有 OpenSandbox 服务的配置方法见 [OpenSandbox 部署指南](providers/opensandbox.md)。

## 在单台 Docker 主机上运行 {#run-on-one-docker-host}

部署会启动以下组件：

| 组件 | 用途 |
|---|---|
| AstraBox API 与控制台 | 创建和使用 Agent、Assistant、Session 与触发配置 |
| PostgreSQL | 保存 AstraBox 和 LiteLLM 数据 |
| LiteLLM | 转发模型请求并发现可用模型 |
| 消息网关 | 把 Agent 接入消息平台 |
| OpenSandbox | 创建和管理本地沙箱容器 |

AstraBox 容器能够控制挂载的 Docker 守护进程。请把它作为主机上的高权限服务管理，不要将本地部署直接暴露到不可信网络。

### 安装发布版本 {#install-a-release}

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh | bash
```

安装脚本会：

1. 检查 Docker、Compose 插件 v2 或更高版本，以及当前用户能否使用
   `/var/run/docker.sock`；
2. 下载最新发布版本的部署包，校验其 SHA-256，并把 Compose 文件解压到
   `~/astrabox`；
3. 在 `~/astrabox/.astrabox/database-secrets` 中一次性生成数据库与登录密钥，
   之后一直沿用；
4. 询问 Agent 使用哪个模型服务，以及它的 API Key 和模型 ID，并写入相应设置；
5. 拉取该版本发布的镜像、启动服务并等待控制台页面可以打开，再拉取内置 Claude Code
   Agent 使用的沙箱镜像，避免第一个 Session 等待下载。

升级时再次运行：它会安装最新发布版本的 Compose 文件（设置了 `ASTRABOX_VERSION` 时
安装该版本），把镜像版本设为该版本，并保留数据卷、已生成的密钥、模型设置，以及设置
文件中的其他所有行。需要更换模型服务时，在它再次询问时重新选择，或设置
`ASTRABOX_INSTALL_MODEL_PROVIDER` 后运行；它为原有服务写入的设置会被删除。

### 安装脚本可配置的模型服务 {#model-services-the-installer-configures}

下列模型服务都由内置的 LiteLLM 网关提供。安装完成后，可以按
[连接模型服务](models.md)添加更多路由。

| 选项 | 写入的设置 | 网关路由的模型名 |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL` | 模型 ID，经 `claude-*` 路由 |
| DeepSeek | `ANTHROPIC_BASE_URL`（DeepSeek 的 Anthropic 端点）、`ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL`（默认 `deepseek-flash`） | `anthropic/<模型 ID>` |
| 其他 Anthropic 兼容服务 | `ANTHROPIC_BASE_URL`、`ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL` | `anthropic/<模型 ID>` |
| OpenAI 兼容服务 | `OPENAI_COMPATIBLE_BASE_URL`、`OPENAI_COMPATIBLE_API_KEY`、`ANTHROPIC_MODEL` | `openai-compatible/<模型 ID>` |
| 暂不配置 | 不写入任何设置 | 之后在控制台的**集成服务**中添加路由 |

`ANTHROPIC_MODEL` 是部署的默认模型：内置 Agent 在选择自己的模型之前都使用它；Agent
自行选择模型时，填写最后一列的模型名。设置了 `ANTHROPIC_BASE_URL` 时，部署通过网关的
`anthropic/` 路由提供默认模型。选择 DeepSeek 时，部署还会识别该端点，并把同一个凭证
提供给网关中 DeepSeek 的 OpenAI 协议路由，供其他 Agent 程序使用。

`openai-compatible/*` 路由通过 Chat Completions 把 `<模型 ID>` 发送到
`OPENAI_COMPATIBLE_BASE_URL`。全新安装内置的 Agent 程序是 Claude Code，它使用
Anthropic Messages 协议，因此 LiteLLM 会在两种协议之间转换；如果该服务同时提供
Anthropic 兼容端点，优先使用它。

### 安装脚本的设置 {#installer-settings}

设置 `ASTRABOX_INSTALL_MODEL_PROVIDER` 后，安装脚本不再提问，可用于无人值守安装。

| 变量 | 用途 |
|---|---|
| `ASTRABOX_VERSION` | 要安装的版本，默认安装最新发布版本。 |
| `ASTRABOX_INSTALL_DIR` | 安装目录，默认 `~/astrabox`。 |
| `ASTRABOX_INSTALL_BUNDLE` | 已下载的 `astrabox-deploy-<version>.tar.gz` 路径，供无法访问 GitHub 的主机使用。 |
| `ASTRABOX_IMAGE_PREFIX` | 保存同一批镜像的镜像仓库前缀，组件名会追加在其后，默认 `ghcr.io/colton-z/astrabox-`。 |
| `ASTRABOX_INSTALL_MODEL_PROVIDER` | `anthropic`、`deepseek`、`anthropic-compatible`、`openai-compatible` 或 `none`。 |
| `ASTRABOX_INSTALL_MODEL_API_KEY` | 模型服务的 API Key。 |
| `ASTRABOX_INSTALL_MODEL_NAME` | 内置 Agent 使用的模型 ID。DeepSeek 默认为 `deepseek-flash`。 |
| `ASTRABOX_INSTALL_MODEL_BASE_URL` | Anthropic 兼容或 OpenAI 兼容服务的 Base URL。 |

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh \
  | ASTRABOX_INSTALL_MODEL_PROVIDER=deepseek \
    ASTRABOX_INSTALL_MODEL_API_KEY="your-deepseek-api-key" bash
```

Compose 设置保存在 `~/astrabox/containers/.env`，升级时会保留。修改端口、卷名或其他
Compose 配置后，再次运行安装脚本即可生效：

```bash
printf '%s\n' "ASTRABOX_SERVER_HOST_PORT='9000'" >> ~/astrabox/containers/.env
```

在该目录下可以使用常规 Compose 命令管理已安装的服务：`docker compose ps`、
`docker compose logs -f server` 和 `docker compose down`。

### 发布的镜像 {#published-images}

每个发布版本都会推送下列镜像，并以版本号作为标签。安装脚本把该版本写入设置文件的
`ASTRABOX_IMAGE_TAG`；未指定镜像的部署，其服务端、各 Agent 程序的沙箱和工作区挂载
辅助程序都运行 `<ASTRABOX_IMAGE_PREFIX><component>:<ASTRABOX_IMAGE_TAG>`。

| 镜像 | 架构 |
|---|---|
| `ghcr.io/colton-z/astrabox-server` | amd64、arm64 |
| `ghcr.io/colton-z/astrabox-sandbox-claude-code` | amd64、arm64 |
| `ghcr.io/colton-z/astrabox-sandbox-codex` | amd64、arm64 |
| `ghcr.io/colton-z/astrabox-sandbox-deepseek-harness` | amd64、arm64 |
| `ghcr.io/colton-z/astrabox-sandbox-hermes` | amd64、arm64 |
| `ghcr.io/colton-z/astrabox-sandbox-pi` | amd64、arm64 |
| `ghcr.io/colton-z/astrabox-workspace-mounter` | amd64 |

工作区挂载辅助程序只有 amd64：它安装的 mergerfs 发布包只面向该架构。

### 从源码运行 {#run-from-a-clone}

从检出的源码运行时，使用本地构建的镜像 `astrabox/<component>:latest`：

```bash
make build-agent-image

export ANTHROPIC_API_KEY="your-anthropic-api-key"
export ANTHROPIC_MODEL="your-model-name"
scripts/compose.sh up --build -d
```

打开 <http://127.0.0.1:8088>。

在 Linux 上，`scripts/compose.sh` 会自动识别 Docker Socket 所属组。如果当前 Docker 安装无法被正确识别，请在启动前设置数字组 ID：

```bash
export DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
scripts/compose.sh up --build -d
```

在 macOS 上，请使用 `stat -f %g` 读取 Socket 所属组，或直接依赖 Docker Desktop 的 Socket 权限。

## 对话工作区保存位置 {#where-conversation-workspaces-live}

工作区文件持久化是可选能力。默认存储适配器为
`ASTRABOX_STORAGE_PROVIDER=mounted_volume`。启用持久化时，通过
`ASTRABOX_SANDBOX_WORKSPACE_VOLUME` 指定已有的 Kubernetes PersistentVolumeClaim
或 Docker named volume，各工作区使用该文件系统中的独立目录。平台在用户沙箱之外通过
mergerfs 创建固定的工作区视图，OpenSandbox 使用标准 PVC 或 named volume 接口挂载
这些视图。在交给用户之前，平台将视图绑定到新建或已有的工作区目录。存储适配器选择
底层介质，工作区路由由平台统一完成。

未设置或设为空值时，工作区文件保存在沙箱的临时文件系统中；替换沙箱可能丢失这些文件。
这项配置只影响工作区文件，原生 SessionStore 仍由平台数据库保存。无卷部署不会启动挂载
辅助程序，与 `ASTRABOX_WORKSPACE_MOUNTER_IMAGE` 的取值无关。

### 配置工作区挂载辅助程序

`ASTRABOX_WORKSPACE_MOUNTER_IMAGE` 指定辅助程序镜像；未设置时使用本版本发布的
`ghcr.io/colton-z/astrabox-workspace-mounter:<version>`。修改辅助程序时，用
`make build-workspace-mounter-image` 构建并发布不可变镜像，再把该变量指向它。这是独立
的主机侧辅助程序，不是 Agent 程序的一部分。

只有一个可运行沙箱的主机时，可以设置 `ASTRABOX_WORKSPACE_STORAGE_TOPOLOGY=local`，
使用本地 ext4 等文件系统。多个沙箱主机必须使用 `shared`，且每台候选主机都能访问同一
远端文件系统；不同本地磁盘上的同名目录不是共享存储。仅增加 API 副本不会改变这个要求。

`ASTRABOX_WORKSPACE_MOUNT_ROOT` 指定主机上的分配视图目录，默认值为
`/var/lib/astrabox/workspace-mounts`，应与工作区数据分开。每个辅助程序只接收自己的
视图目录，使用双向挂载传播。Docker 部署需先把所在主机挂载设为 shared，否则 Docker
会拒绝从非共享源传播 `rshared` 挂载。

辅助程序要求 amd64、Linux 6.9+、root、`/dev/fuse` 和特权挂载权限；这些权限不会授予
用户沙箱。它要求原生 mergerfs I/O passthrough 和兼容的缓存模式，不会静默关闭 passthrough。

Kubernetes 中的平台身份需要管理辅助 Pod 及其 exec 接口、分配视图 PVC/PV，并能列出
Node；命名空间必须允许这些特权基础设施 Pod。底层 PVC 仍由部署方管理。Kubernetes
调度辅助 Pod，视图 PV 则把沙箱限定在同一节点。辅助程序消失后，已有 FUSE 挂载失效，
不能靠在运行中的沙箱下重启辅助程序恢复。Docker 视图数据卷使用递归绑定，让沙箱能够
看到其中的 FUSE 子挂载；非递归绑定只会暴露空的主机目录，挂载检查会拒绝这种状态。

### 存储介质必须支持的操作

AWS EFS 使用 [`aws_efs` 存储适配器](providers/aws-efs.md)，它检查实际的 EFS CSI
claim，仍使用同一个平台路由。工作区是常规工作目录，不是文档存储。存储介质必须支持
以下操作：

| 操作 | 用途 |
|---|---|
| 覆盖已有路径的 `rename` | 逐个发布 Plugin 仓库缓存文件，避免其他对话读取未完成的版本 |
| `chmod`（保留 mode） | 保留仓库中标记为 `100755` 的可执行 Plugin 脚本 |
| `flock` | 串行处理同一 Agent 共享缓存的并发 clone |
| 符号链接 | 将对话可见的 `/workspace` 指向实际工作目录，并在挂载检查中解析该路径 |
| `ReadWriteMany` | 允许多个沙箱同时挂载数据卷，并让一个 Agent 的沙箱承载多个对话 |

| 存储介质 | 结论 |
|---|---|
| NFS、EFS、CephFS 等 POSIX 网络文件系统 | 必须支持上表操作，文件系统类型须被挂载辅助程序识别，且 PVC 能在消费者启动前绑定。不要让 NFS 服务与挂载它的工作负载运行在同一节点。 |
| 集群默认 StorageClass | 取决于实际供应的存储。使用前确认它支持 POSIX 语义且不是节点本地存储。 |
| 通过 FUSE 挂载的对象存储 | 只有确认支持上表全部操作后才能使用；Mountpoint for Amazon S3 不符合要求。 |

在 `shared` 拓扑中，辅助程序识别 `nfs`、`nfs4`、`cifs`、`smb3`、`ceph`、`fuse.ceph`、
`glusterfs`、`fuse.glusterfs`、`lustre`、`gpfs`、`beegfs` 和 `fuse.juicefs`。其他类型会
被拒绝，即使驱动声明支持 POSIX。类型被识别也不能代替对实际部署所需文件操作的检查。

Mountpoint for Amazon S3 说明：通用 bucket 不支持文件 rename，任何 bucket 类型都不支持
目录 rename，并且不支持 `chmod`、`lockf`、硬链接和符号链接。这些限制与上述工作区操作
冲突。详见它的[文件系统行为](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)。

快照、导出和备份仍可以使用对象存储，因为这些数据会作为完整对象一次写入；实时工作区
需要文件系统语义。

OpenSandbox 还定义了 `ossfs` volume 类型，但 AstraBox 不会选择它。配置工作区持久化后，
所有沙箱创建路径（包括准备容量）都使用平台的固定视图挂载方案，底层文件系统由所选
存储 provider 提供。AstraBox 只接受为部署配置的存储 provider。

### 部署前检查

在 Kubernetes 中启用工作区持久化时，应准备名称与
`ASTRABOX_SANDBOX_WORKSPACE_VOLUME` 一致、已绑定且支持 `ReadWriteMany` 的
PersistentVolumeClaim。存储辅助程序挂载这个底层数据卷，OpenSandbox 挂载分配给沙箱的
已就绪视图数据卷。配置的存储 provider 无法确认所需挂载时，AstraBox 会拒绝准备运行环境。

## 连接消息平台

服务镜像已经包含固定版本的 Satori adapter 和内置连接程序。在 Agent 页面创建消息平台
Deployment 并填写 provider 表单；详情页会显示回调地址或连接状态，并提供对应的官方
开发者控制台链接。

单服务部署通过容器 loopback 使用内置连接程序。多副本部署可以在经过认证的 HTTPS
地址后运行一个共享连接程序：将 `ASTRABOX_CHANNEL_GATEWAY_BASE_URL` 设置为该地址，
并在 AstraBox 和连接程序上设置相同的 `ASTRABOX_CHANNEL_GATEWAY_TOKEN`。要求见
[仓库中的配置参考](https://github.com/Colton-z/AstraBox/blob/main/docs/configuration.md)。

## 运行多个平台副本

[平台多副本部署指南](deploy-distributed.md)说明如何让多个 API 主机连接共享服务并使用
相同的签名密钥。请求可以到达任一 API 副本，沙箱由 Kubernetes 独立调度。增加 API
副本不会让本地工作区磁盘变成共享存储，也不会让单个 OpenSandbox 生命周期服务自动获得高可用能力。

## 构建沙箱镜像

Environment（运行环境）把 Agent 与 Agent 程序及兼容的沙箱镜像关联起来。Agent 可用的操作系统、CPU 架构、Agent 程序、命令和语言运行时都来自该镜像。

如果每个 Session 都需要额外软件，请以对应的内置沙箱镜像为基础构建自定义镜像，安装并固定依赖，再在 Environment 中选择这个镜像。生产部署应使用不可变的镜像标签。

沙箱所需的 Agent 程序、账号和控制进程必须在沙箱启动时已经存在。不要依赖每个 Session 单独执行的安装脚本：预热沙箱可能早于使用它的 Session 创建。

内置沙箱镜像使用 `/workspace` 作为 Session 工作区。Session 的文件页面读写的就是这个目录。自定义镜像必须确保 Agent 程序可以写入所配置的工作目录。镜像要求见[容器参考](container-reference.md)。

## 保存和恢复数据

仓库维护的 Compose 部署会把服务数据与 Agent 沙箱分开保存：

| 位置 | 内容 |
|---|---|
| `astrabox-postgres` 数据卷 | AstraBox 和 LiteLLM 数据库 |
| `astrabox-state` 数据卷 | `/data` 状态、生成的密钥和本地 OpenSandbox 元数据 |
| `.astrabox/database-secrets` | 生成的数据库凭证和可选的内置 SSO 凭证，位于安装目录（`~/astrabox`）或源码检出目录下 |
| 可选工作区数据卷 | 独立于沙箱的 Agent 和 Assistant 工作区文件 |

Session 消息和原生会话状态保存在 AstraBox 数据库中。配置工作区数据卷后，文件独立保存，
终止或替换沙箱不会删除它们，替代沙箱会挂载原工作区。没有工作区数据卷时，文件依赖原
沙箱或保留的文件系统快照。OpenSandbox 普通暂停保留根文件系统，不恢复进程内存；它
不是卷上工作区文件或数据库原生会话状态的持久化机制。

请把以下内容作为一组恢复数据一起备份：

- `astrabox-postgres`；
- `astrabox-state`；
- `.astrabox/database-secrets`；
- 解密已保存凭证所需的本地 Vault 密钥或 KMS 密钥；
- 已配置的工作区数据卷或底层文件系统；
- 已配置的外部 LiteLLM 存储。

默认的本地 Credential Vault 密钥来自 `ASTRABOX_VAULT_MASTER_KEY` 或 `/data/vault.key`。没有同一个密钥，就无法恢复已加密的凭证。

## 开放给团队使用

AstraBox 支持 OIDC、认证网关传入的可信身份请求头，以及 JWT 验证。已安装的部署在设置文件中
配置其中一种，可用设置见[设置团队登录](team-login.md)。内置 SSO 叠加配置会启动 Casdoor 供
评估使用，它从源码检出运行：

```bash
scripts/compose.sh -f containers/compose.sso.yaml up -d
```

内置 Casdoor 应用使用 `/static/astrabox-mark.svg` 作为登录 Logo。叠加配置把仓库中已有
品牌资源挂载到 Casdoor 静态目录，不依赖外部 CDN。Casdoor 只在初始化时导入应用数据；
已有数据库需要在应用设置的 Logo 字段中填写该路径。AWS 测试床部署时会通过 Casdoor API
同步控制台应用的 Logo。

团队入口需要终止 TLS，把公网主机名加入 `ASTRABOX_ALLOWED_HOSTS`，禁止直接访问 AstraBox 服务端口，并让所有副本使用相同的登录 Cookie 签名密钥。受支持的身份配置见[设置团队登录](team-login.md)。

## 检查健康状态和沙箱恢复

AstraBox 分别提供存活与就绪检查：

```text
GET /healthz
GET /readyz
```

服务开始排空并准备关闭后，`/readyz` 会返回 `503`。平台的终止宽限时间应大于 `ASTRABOX_SHUTDOWN_DRAIN_SECONDS`，让正在执行的 Agent 任务有时间完成。

CPU、内存、磁盘和沙箱超时由所选 OpenSandbox 运行时及其配置决定。依赖暂停沙箱之前，请在实际部署的后端上验证每个使用暂停功能的 Environment：

```bash
astrabox verify-opensandbox-snapshots
```

## 接入模型、消息平台和凭证

- [连接模型服务](models.md)。
- [把 Agent 接入消息平台](channels.md)。
- [保护 Agent 使用的凭证](egress-credential-injection.md)。

## 上线检查清单

邀请用户前：

- 启用认证，并核验用户和机器访问的鉴权；
- 在可信入口终止 TLS，并设置 `ASTRABOX_ALLOWED_HOSTS`；
- 所有副本使用共享持久化，以及相同的签名密钥和加密密钥；
- 将数据库数据、状态、凭证和加密密钥一起备份；
- 验证沙箱可以访问模型网关、AstraBox 回调、远程 MCP Server 和其他必需地址；
- 验证每个会暂停沙箱的 Environment 都能从快照恢复；
- 设置工作负载需要的出站网络规则和沙箱权限；
- 在测试正常关闭时运行一次真实 Agent 任务；
- 收集日志、指标和链路追踪数据，并明确访问与保留规则。

## 相关指南

- [Environment](environments.md)
- [OpenSandbox](providers/opensandbox.md)
- [团队登录](team-login.md)
- [容器参考](container-reference.md)
