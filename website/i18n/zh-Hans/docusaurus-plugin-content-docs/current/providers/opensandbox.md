# OpenSandbox

OpenSandbox 是 AstraBox 内置的沙箱服务。[Environment（运行环境）](../environments.md)
决定 Agent 使用的沙箱镜像、网络访问和生命周期选项。Session 启动时，AstraBox 会创建
或取得一个 OpenSandbox 沙箱，让 Agent 程序在 `/workspace` 中工作；对话和其他产品数据
保存在沙箱之外。

OpenSandbox 提供沙箱生命周期、命令执行、文件操作和服务地址。AstraBox 使用这些能力让
Agent 在部署方管理的基础设施中运行；开发者关闭自己的电脑后，Agent 仍可继续工作。
用户可以随时从管理台、API 或已经配置的集成中回来继续使用它。

## 选择部署方式

| 部署方式 | 适用场景 | 沙箱运行位置 |
|---|---|---|
| 内置 Docker | 评估、开发和单台可信主机 | AstraBox 主机上的独立容器 |
| Kubernetes | 多节点、预热容量、快照和集群管理 | OpenSandbox 控制器管理的 Pod |
| 已有 OpenSandbox 服务 | 单独维护沙箱基础设施的组织 | 该服务配置的 Docker 或 Kubernetes 运行时 |

项目维护的 Compose 部署使用内置 Docker。单机启动方式和备份范围见
[部署 AstraBox](../deploy.md)。

OpenSandbox 也维护自己的[服务部署](https://open-sandbox.ai/components/server)和
[Kubernetes 部署](https://open-sandbox.ai/kubernetes/deployment)文档。AstraBox 使用
OpenSandbox Lifecycle API，不会取代这些文档中的控制器、容器运行时或集群网络。

## 在 Kubernetes 中运行沙箱

先安装 OpenSandbox 控制器和 CRD，再创建沙箱 Pod 所在的命名空间。AstraBox 会检查
命名空间、所选 workload CRD、Kubernetes API 访问和必要权限，但不会创建集群级资源
或命名空间。

项目维护的 Compose overlay 让 AstraBox 在集群外运行，并在集群内创建沙箱 Pod。将
kubeconfig 复制到容器可读的位置，再提供从两侧网络都有效的地址：

```bash
sudo install -o 999 -g 999 -m 600 "$HOME/.kube/config" \
  "$HOME/astrabox-kubeconfig"

export ASTRABOX_KUBECONFIG_HOST_PATH="$HOME/astrabox-kubeconfig"
export ASTRABOX_SANDBOX_SERVER_KUBE_API_SERVER=https://10.0.1.7:6443
export ASTRABOX_SERVER_BIND_IP=10.0.1.7
export ASTRABOX_MCP_PROXY_BASE_URL=http://10.0.1.7:8088
export ASTRABOX_ALLOWED_HOSTS=10.0.1.7
export ASTRABOX_LITELLM_BASE_URL=https://llm.example.com

scripts/compose.sh -f containers/compose.kubernetes.yaml up -d
```

这些地址分别用于不同路径：

- AstraBox 必须能访问 Kubernetes API 地址，且该地址必须包含在 TLS 证书中；
- 沙箱 Pod 必须能访问 AstraBox 回调地址和模型网关；
- 浏览器使用的域名必须包含在 `ASTRABOX_ALLOWED_HOSTS` 中。

经过测试的 workload 类型是 `batchsandbox`，对应
`batchsandboxes.sandbox.opensandbox.io`。使用 Agent Sandbox CRD 的安装可以改为设置
`ASTRABOX_SANDBOX_SERVER_KUBE_WORKLOAD_PROVIDER=agent-sandbox`。

每个被选中的沙箱镜像都要发布到集群可访问的镜像仓库。使用不可变标签或 digest，保证
新建 Pod 和预热 Pod 使用相同软件。需要临时新增节点的集群还可能需要调大
`ASTRABOX_SANDBOX_SERVER_KUBE_CREATE_TIMEOUT_SECONDS`，为节点启动和镜像拉取留出时间。

## 连接已有 OpenSandbox 服务

在 AstraBox 服务进程环境中设置 Lifecycle API 地址及其具名 API key：

```bash
ASTRABOX_SANDBOX_OPENAPI_BASE_URL=https://sandbox-control.example.com
ASTRABOX_SANDBOX_API_KEY_SECRET_NAME=opensandbox-api-key
OPENSANDBOX_API_KEY=REPLACE_WITH_LIFECYCLE_API_KEY
```

secret provider 读取进程环境，不读取产品的加密 Vault。它将引用名称大写，并把连字符
替换成下划线。容器部署必须显式把这些配置注入服务容器；只在主机上导出变量不会让
内置 Compose 文件自动传入它们。[平台多副本指南](../deploy-distributed.md)包含外部
Kubernetes 生命周期服务所需的环境文件接入方法。

AstraBox 必须同时能访问 Lifecycle API 以及该 API 返回的沙箱地址。只要 OpenSandbox
服务不局限在可信回环网络，就必须启用 API Key 认证。内置生命周期服务只在 AstraBox
容器内监听，不需要再开放一个公网地址。

## 准备沙箱镜像

沙箱镜像包含 Agent 程序、系统软件包、账户，以及使用该镜像的每个 Session 都需要的
后台服务。内置镜像使用 `/workspace` 作为 Session 工作区。

如需增加通用软件，请以对应的内置镜像为基础构建自定义镜像，再到
**管理台 → Environment → 沙箱镜像或模板**中选择它。
不要依赖每个 Session 单独执行的初始化脚本：预热沙箱可能在使用它的 Session 出现之前
就已经创建。

AstraBox 使用 OpenSandbox 原生命令、文件系统和端点 API。内置镜像仍以 AIO 负责启动和
工作负载账户生命周期，最终进入 `/opt/gem/run.sh`；自定义基础镜像需要提供等效生命周期，
并配置匹配的 entrypoint。

[容器参考](../container-reference.md)说明镜像与工作区约定。AstraBox 创建的每个沙箱都使用
平台统一的资源规格：运行上限为 `4` CPU 和 `4Gi` 内存，调度请求为 `200m` CPU 和
`768Mi` 内存。冷创建和预备容量使用同一份配方，而不是为单个 Session 设置 Environment
资源。磁盘、GPU 和集群准入仍由 OpenSandbox 部署管理。

## 使用预热容量

在 Agent 上启用预热，可以在用户开始对话之前准备好完整运行时。两种 tenancy 都使用
OpenSandbox 官方 SDK client pool 完成创建、协调、补位、重试和原子领取。Agent tenancy
领取的箱子用于 Agent 共享，保留已有根目录，并为各会话分配隔离目录；conversation
tenancy 则由领取的 Session 独享整箱，配置持久化存储时，平台在激活预热运行时之前，
将工作区视图绑定到新建或已有的 Session 目录。两种模式都不会更换运行中 Session 的
工作区。多进程或多机器部署通过
`ASTRABOX_AGENT_PREWARM_REDIS_URL` 共享 SDK client pool 状态。

两种模式都在引擎准备好后才报告可用容量，领取时激活已准备的运行时。独享模式在 SDK
发布池成员之前完成准备，临时交接记录放在箱子的私有 home 中，不进入 Workspace。
共享模式在常驻箱子里准备隔离的引擎位置。这些交接记录都不是持久的 SessionStore。

修改独享模式 Agent 的启动配置会替换尚未领取的预热容量，已领取的箱子仍属于原 Session。
共享模式中，可以注入的配置变更只替换等待中的引擎位置，不替换常驻箱子。

平台决定 Agent 版本、持久工作区挂载、凭证和启动要求。OpenSandbox 提供沙箱生命周期
与池能力，不参与 AstraBox 的用户或 Session 流程。

Environment 可以为每段对话分配独立沙箱，也可以让同一 Agent 的多段对话共用沙箱。
共用模式仍为每段对话提供独立的 Linux 用户和工作区，但它们会共用容器和网络命名空间。
因此，该模式需要高级沙箱权限，以及已经为 OpenSandbox 隔离 Session 配置好的部署。

在共享沙箱模式下，每个内置 Agent 程序都会让每段对话在自己的账户中运行。沙箱无法
证明自己支持隔离 Session 时，AstraBox 会拒绝使用它，不会退回到未隔离的运行方式。

## 暂停与恢复沙箱

OpenSandbox 部署支持快照时，Environment 可以在沙箱闲置后暂停它，而不是直接终止。
暂停会把沙箱根文件系统写入 OCI 镜像并释放计算资源；恢复后沙箱 ID 和文件保持不变，
但会启动新的进程，普通进程内存不会恢复。

受支持的 Kubernetes 路径需要：

- OpenSandbox 快照控制器和 image committer；
- 能够访问沙箱 Pod 所用的 containerd socket；
- 可供快照推送和拉取的 OCI 镜像仓库；
- 使用私有仓库时，在沙箱命名空间中提供镜像仓库凭证。

在生产 Environment 中启用暂停之前，先验证写入、暂停、恢复和再次读取的完整路径：

```bash
astrabox verify-opensandbox-snapshots
```

还要为快照仓库设置保留策略。删除 OpenSandbox 快照元数据本身不会清理 OCI 镜像数据。
控制器和镜像仓库要求见 OpenSandbox 的
[Pause and Resume](https://open-sandbox.ai/guides/pause-resume) 文档。

没有工作区数据卷时，文件依赖同一个沙箱或已验证的文件系统快照。配置数据卷后，文件
保存在底层文件系统中，独立于沙箱。两种模式的原生会话状态都保存在 AstraBox 数据库中。
Assistant 休眠保存原生状态后释放沙箱，不等于 OpenSandbox 暂停。参见
[Assistant](../assistants.md)和[工作区存储](../deploy.md#where-conversation-workspaces-live)。

## 配置网络与凭证保护 {#credential-protection}

在**管理台 → Environment → 网络访问**中允许全部出站地址，或只允许 Agent 需要的
主机。AstraBox 会将该设置与模型地址、平台回调地址、声明的 Plugin 来源、允许的远程
MCP 服务，以及已分配 Credential 授权的地址合并。[IP 地址](../networking.md)说明防火墙
和固定出口的配置方法。

项目维护的部署默认把模型、远程 MCP 和外部 API 的真实凭证保存在沙箱外：

```bash
ASTRABOX_SANDBOX_CREDENTIAL_VAULT=true
ASTRABOX_SANDBOX_EGRESS_MODE=dns+nft
```

OpenSandbox 出站代理只向 Agent 程序提供不透明占位符，并且只在出站请求符合规则时加入
真实值。冷启动和预备容量使用同一条标准 OpenSandbox 创建路径：AstraBox 提供有效网络
策略并启用代理，OpenSandbox 随后配置代理，并向 SDK 返回每个沙箱各自的端点认证信息。
AstraBox 不配置部署级出站令牌。支持的请求匹配方式和验证流程见
[保护 Agent 使用的凭证](../egress-credential-injection.md)。

对于支持 HTTP Basic 的 HTTPS 服务（包括私有 Git 仓库），在 AstraBox Vault 中创建
`http_basic` Credential，并把 Vault 分配给 Agent。填写不含凭证的仓库 URL、用户名以及
只写的密码或 token。provider 将它转换为 OpenSandbox 原生 `auth.type="basic"` 绑定，
不在沙箱里安装 Git 凭证 helper、含 token 的 clone URL 或密钥环境变量。冷启动和预热
路径都会先准备凭证绑定，再下载 Skill 与 Plugin。

该绑定只匹配 HTTPS 443 端口、指定路径及其子路径，以及 `GET`、`HEAD`、`POST` 方法，
不会向其他仓库路径注入凭证。这是通用鉴权方式，不是 Git 专属的引擎能力。配置方法见
[Vault 凭证](../credentials.md)和 OpenSandbox 的
[原生 Git 凭证指南](https://github.com/opensandbox-group/OpenSandbox/blob/2f03f68c25644a68fee31d0759915de0b104b4ca/docs/guides/credential-vault.md#git-and-curl-with-vault-injected-credentials)。

## 选择沙箱运行时

`ASTRABOX_SANDBOX_SECURE_RUNTIME` 为一个部署选择一种容器运行时。留空表示使用 runc；
也可以选择宿主机或集群中已经安装的运行时：

| 运行时 | AstraBox 中的 OpenSandbox 支持范围 |
|---|---|
| runc | 通用任务、受限网络和凭证保护 |
| gVisor | 额外的系统调用隔离；不能使用 OpenSandbox 网络和 Credential Vault |
| Kata | 基于虚拟机的隔离，同时支持网络和 Credential Vault |
| Firecracker | 仅限 Kubernetes，并且需要匹配的 Kata/Firecracker `RuntimeClass` |

OpenSandbox 出站代理需要 `iptables` NAT table，而 gVisor 不提供该能力。请求同时使用
gVisor 和 OpenSandbox 网络规则时，沙箱会被拒绝，不会在缺少网络规则的情况下启动。
Agent 需要不同运行时策略时，请使用不同部署。当前安装要求见 OpenSandbox 的
[Secure Container Runtime](https://open-sandbox.ai/guides/secure-container) 文档。

## 安全发布沙箱服务

AstraBox 可以直接连接 OpenSandbox 返回的地址，也可以让 Lifecycle API 转发 HTTP、
SSE 和 WebSocket。项目维护的 Docker 部署使用转发方式，因为 AstraBox 在容器内运行，
而发布的端口属于宿主机。

Kubernetes 部署可以直接路由到 Pod，也可以使用 OpenSandbox ingress 组件。向不可信
网络开放沙箱服务前，请启用 OpenSandbox Secure Access，并为 AstraBox 和 ingress
组件配置相同的签名密钥。AstraBox 会先检查 Session 鉴权，再签发短期访问地址。

## 检查部署

打开**管理台 → 沙箱**，查看每个沙箱报告的状态、镜像、到期时间、网络规则、Credential
和诊断信息。

| 现象 | 检查项 |
|---|---|
| Pod 一直 Pending | 控制器和 CRD 状态、沙箱命名空间、节点容量、镜像引用和仓库访问 |
| Kubernetes API 报告 TLS 错误 | 到 API 地址的网络路径，以及证书包含的地址 |
| 沙箱已经就绪，但 Session 仍在等待 | AstraBox 到 Agent 服务的网络路径，包括直连和转发方式 |
| Agent 准备容量一直不可用 | `prepared-runtime` 状态、镜像、entrypoint、权限和 Redis 连接 |
| 恢复后的沙箱缺少文件 | 快照控制器、containerd socket、镜像仓库凭证和快照验证命令 |

## 相关文档

- [部署 AstraBox](../deploy.md)
- [Environment](../environments.md)
- [容器参考](../container-reference.md)
- [保护 Agent 使用的凭证](../egress-credential-injection.md)
