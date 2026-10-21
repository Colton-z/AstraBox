# 部署 AstraBox

AstraBox 是自托管软件。你可以在一台 Docker 主机上运行完整服务，也可以把 AstraBox 接入 Kubernetes 中的 OpenSandbox，或者使用组织已经运行的 OpenSandbox 服务。仓库维护的单机部署会启动 Web 控制台、API、数据存储、模型与消息网关以及本地沙箱服务；Agent 任务在相互隔离的沙箱中运行。

![AstraBox 部署方式](./img/deploy-topology.svg#inline)

## 选择 Agent 沙箱的运行位置

| 部署方式 | 适用场景 | 沙箱运行位置 |
|---|---|---|
| 单台 Docker 主机 | 评估、开发和小型可信团队 | 主机 Docker 守护进程创建的独立容器 |
| Kubernetes 与 OpenSandbox | 多节点、预热沙箱、快照和集群管理 | OpenSandbox 创建的 Sandbox Pod |
| 已有 OpenSandbox 服务 | 组织单独运行沙箱基础设施 | 该服务配置的容器运行时 |

仓库维护的本地部署只监听本机回环地址，启用[团队登录](team-login.md)之前不进行认证。需要从其他网络访问 AstraBox 时，请先配置团队登录、TLS 和可信入口。

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
| Casdoor（启用团队登录时） | 让用户登录控制台 |

请把服务端容器作为主机上的高权限服务管理：它能够控制挂载的 Docker 守护进程。

### Docker 版本要求 {#docker-requirements}

| 要求 | 原因 |
|---|---|
| Docker Engine 26.0 或更高版本（Engine API 1.45） | 持久工作区通过挂载 Docker 数据卷的子目录（`VolumeOptions.Subpath`）进入每个沙箱，这是 Engine API 1.45 新增的能力。部署用到的其他功能都更早。 |
| Docker Compose 插件 2.17.0 或更高版本 | 沙箱边缘容器重建时，`containers/compose.yaml` 会重启服务端（`depends_on` 中的 `restart: true`），这是 Compose 2.17.0 引入的。 |

安装脚本和服务端都会检查 Docker 守护进程，版本过旧时拒绝继续并给出实际版本；服务端每次启动
都会检查，因此升级主机后，服务端也不会在不支持的 Docker 版本上运行。

### 沙箱网络边界 {#sandbox-network-boundary}

每个沙箱都是 Docker 默认 `bridge` 网络上的一个容器，并配有一个执行其 Environment
网络设置的 OpenSandbox 出站边车。这套部署让平台不出现在该网络上：

- 服务端只在 `127.0.0.1` 上发布控制台。PostgreSQL 和 Redis 只发布本机回环的开发端口。
- `sandbox-edge` 和 `sandbox-dns-edge` 两个边缘容器在 bridge 上有自己的地址，只把模型路由、
  按能力授权的回调路由和 DNS 转发给服务端；转发经过私有网络 `sandbox-edges`，该网络没有
  通往主机之外的路由，沙箱也从不加入。
- 生命周期服务把每个沙箱的 execd、文件/终端和出站 API 端口发布在 Docker bridge 网关上
  （`ASTRABOX_PUBLISH_HOST_IP`，安装脚本从 `docker network inspect bridge` 读取），不发布到
  主机的其他网卡。沙箱内的这些服务没有认证：除非前面有对每个请求做认证的服务，否则不要把
  `ASTRABOX_PUBLISH_HOST_IP` 设为 `0.0.0.0`。
- 无论 `受限` 还是 `不受限`，每个沙箱的出站策略都拒绝 Docker 默认 bridge 上除
  `sandbox-edge` 以外的地址，即其他沙箱，以及发布沙箱端口的 bridge 网关。这些拒绝规则先于所有
  允许规则生效，所以 Environment 白名单里写上 bridge 地址也不会重新放开。拒绝列表是
  `ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS`，由服务端启动时推导。出站边车只在 `dns+nft`
  模式下执行地址规则，因此设置 `ASTRABOX_SANDBOX_EGRESS_MODE=dns` 时服务端拒绝启动。
- 无论哪种网络模式，都拒绝存放云厂商元数据服务的 `169.254.0.0/16`。
- 沙箱没有 IPv6。出站边车会在它与沙箱共享的网络命名空间里关闭 IPv6（OpenSandbox 的
  `egress.disable_ipv6`，由 AstraBox 设置），Docker 守护进程在默认 bridge 上启用 IPv6
  时也是如此。因此上面的拒绝规则都是 IPv4 网段。

`不受限` 仍然允许其他所有目标，包括主机的其他地址和你的局域网。不是 AstraBox 沙箱的其他
bridge 容器以及主机上的进程可以访问发布出来的沙箱端口；主机及其其他容器都在信任边界之内。

两个边缘容器的 bridge 地址由 Docker 在它们启动时分配，重启后的边缘容器可能拿到不同的地址。
每个沙箱都保留创建时的地址（DNS 上游、拒绝规则和回调地址），所以平台会把这些地址记录在沙箱上。
服务端以不同的地址启动时，会移除用旧地址创建的沙箱；服务端运行期间发现边缘容器换了地址时会退出，
由 Docker 重新启动它。沙箱被移除的对话在下一条消息时换用新沙箱，与其他沙箱丢失的情况一样；
被移除沙箱工作区里的文件只有在启用可选的持久工作区卷时才会保留。AstraBox 0.1.0
创建的沙箱没有这项记录，不会以这种方式移除。如果边缘容器重启后这些对话收不到回复，
请移除主机上的所有沙箱，每个对话随后会换到新沙箱继续：

```bash
docker ps -aq --filter label=opensandbox.io/id | xargs -r docker rm -f
docker ps -aq --filter label=opensandbox.io/egress-sidecar-for | xargs -r docker rm -f
```

### 安装发布版本 {#install-a-release}

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh | bash
```

安装脚本会：

1. 检查 Docker Engine 26.0 或更高版本、Compose 插件 2.17.0 或更高版本（见
   [Docker 版本要求](#docker-requirements)），以及当前用户能否使用
   `/var/run/docker.sock`；
2. 下载最新发布版本的部署包，校验其 SHA-256，并把 Compose 文件解压到
   `~/astrabox`；
3. 在 `~/astrabox/.astrabox/database-secrets` 中一次性生成数据库与登录密钥，
   之后一直沿用；
4. 询问 Agent 使用哪个模型服务，以及它的 API Key 和模型 ID，并写入相应设置；
5. 询问是否启用团队登录，即随服务一起运行内置的 Casdoor 登录服务；
6. 拉取该版本发布的镜像、启动服务并等待控制台页面可以打开，再拉取内置 Claude Code
   Agent 使用的沙箱镜像，避免第一个 Session 等待下载。

升级时再次运行：它会安装最新发布版本的 Compose 文件（设置了 `ASTRABOX_VERSION` 时
安装该版本），把镜像版本设为该版本，并保留数据卷、已生成的密钥、模型设置、团队登录选择，以及设置
文件中的其他所有行。需要更换模型服务时，在它再次询问时重新选择，或设置
`ASTRABOX_INSTALL_MODEL_PROVIDER` 后运行；它为原有服务写入的设置会被删除。

### 升级匹配的组件

服务端与沙箱运行镜像需要一起升级。进程传输改动还要求匹配的 execd 与沙箱镜像；已有沙箱
会保留安装时的二进制。在 0.1.10 中，内置 PostgreSQL 提供方将结构升级到 Alembic 修订
`0003`，添加可空的 `sessions.runtime_versions`。外部持久化提供方必须提供对应物理结构。
管理员版本页面读取已保存的运行观测，不会唤醒计算资源；参阅
[运行版本观测](writing-an-engine-adapter.md)。

### 从 AstraBox 0.1.8 升级 {#upgrade-from-0-1-8}

AstraBox 0.1.9 使用新的 PostgreSQL 结构，不会自动转换 0.1.8 记录。需要保留选定记录时，
运维方可以在正常启动前转换并加载到当前结构。按[离线导入指南](data-import.md)保留对象顺序、
稳定 ID、鉴权、密钥以及历史与原生状态。转换与执行由运维方负责；没有内置的整套安装导入命令，
也不提供通用旧格式兼容。

以下流程会**丢弃旧 AstraBox 记录并全新开始**。Agent、Environment、Assistant、Session
及历史、Deployment 和 Vault 不会带入。新版本初始化默认 Environment 和 Agent；选择
此路径时，其余资源需重新创建。

新数据库还会让这个安装获得新的标识。AstraBox 只删除、复用和计入本安装自己创建的沙箱，
因此 0.1.8 安装的沙箱会一直运行，直到你删除它们。运行新版本的安装脚本之前：

1. 停止服务端。在 `~/astrabox/containers` 中运行：

   ```bash
   docker compose stop server
   ```

2. 删除 Docker 主机上的沙箱。以下命令会删除该主机上的所有 OpenSandbox 沙箱，包括
   主机上其他 AstraBox 安装的沙箱：

   ```bash
   docker ps -aq --filter label=opensandbox.io/id | xargs -r docker rm -f
   docker ps -aq --filter label=opensandbox.io/egress-sidecar-for | xargs -r docker rm -f
   ```

   沙箱运行在 Kubernetes 或已有的 OpenSandbox 服务上时，请改为在那里删除 0.1.8 安装的
   沙箱。

3. 清空 AstraBox 数据库。在仓库维护的 Compose 部署中，它是 `postgres` 服务的 `astrabox`
   数据库；LiteLLM 和 Casdoor 使用各自独立的数据库，数据会保留：

   ```bash
   docker compose exec postgres psql -U postgres \
     -c 'DROP DATABASE astrabox WITH (FORCE)' \
     -c 'CREATE DATABASE astrabox OWNER astrabox' \
     -c 'REVOKE ALL ON DATABASE astrabox FROM PUBLIC' \
     -c 'GRANT CONNECT, TEMPORARY ON DATABASE astrabox TO astrabox'
   ```

   使用外部 PostgreSQL 服务时，请改为把 `ASTRABOX_DB_URL` 指向一个新的空数据库。

4. 运行新版本的安装脚本。

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

`ANTHROPIC_MODEL` 是部署的默认模型：全新安装用它创建内置 Agent；Agent 自行选择模型时，
填写最后一列的模型名。每个 Agent 都保存自己运行的模型，之后修改 `ANTHROPIC_MODEL`
不会改变已有 Agent 的模型；请在各 Agent 的页面中选择新模型。设置了
`ANTHROPIC_BASE_URL` 时，部署通过网关的
`anthropic/` 路由提供默认模型。选择 DeepSeek 时，部署还会识别该端点，并把同一个凭证
提供给网关中原生的 DeepSeek 路由，这些路由为其他 Agent 程序提供 Messages 和 OpenAI 协议。

`openai-compatible/*` 路由通过 Chat Completions 把 `<模型 ID>` 发送到
`OPENAI_COMPATIBLE_BASE_URL`。全新安装内置的 Agent 程序是 Claude Code，它使用
Anthropic Messages 协议，因此 LiteLLM 会在两种协议之间转换；如果该服务同时提供
Anthropic 兼容端点，优先使用它。

### 安装脚本的设置 {#installer-settings}

设置 `ASTRABOX_INSTALL_MODEL_PROVIDER` 和 `ASTRABOX_INSTALL_TEAM_LOGIN` 后，安装脚本不再提问，可用于无人值守安装。没有终端时，安装脚本保持当前的团队登录选择；新安装默认不启用。

| 变量 | 用途 |
|---|---|
| `ASTRABOX_VERSION` | 要安装的版本，默认安装最新发布版本。 |
| `ASTRABOX_INSTALL_DIR` | 安装目录，默认 `~/astrabox`。 |
| `ASTRABOX_INSTALL_BUNDLE` | 已下载的 `astrabox-deploy-<version>.tar.gz` 路径，供无法访问 GitHub 的主机使用。 |
| `ASTRABOX_IMAGE_PREFIX` | 保存同一批镜像的镜像仓库前缀，组件名会追加在其后，默认 `ghcr.io/colton-z/astrabox-`。 |
| `ASTRABOX_INSTALL_MODEL_PROVIDER` | `anthropic`、`deepseek`、`anthropic-compatible`、`openai-compatible` 或 `none`。 |
| `ASTRABOX_INSTALL_MODEL_API_KEY` | 模型服务的 API Key。 |
| `ASTRABOX_INSTALL_MODEL_NAME` | 写入为 `ANTHROPIC_MODEL` 的模型 ID，全新安装用它创建内置 Agent。DeepSeek 默认为 `deepseek-flash`。 |
| `ASTRABOX_INSTALL_MODEL_BASE_URL` | Anthropic 兼容或 OpenAI 兼容服务的 Base URL。 |
| `ASTRABOX_INSTALL_TEAM_LOGIN` | `casdoor` 表示随服务运行内置的 Casdoor 登录服务；`none` 表示不启用登录。 |
| `ASTRABOX_CONSOLE_ORIGIN` | 启用团队登录时：控制台的公网 URL，例如 `https://astrabox.example.com`。 |
| `ASTRABOX_OIDC_ISSUER` | 启用团队登录时：Casdoor 的公网 URL，例如 `https://login.example.com`。供其他计算机上的浏览器使用时两个 URL 都要设置；只在本机登录时都不设置。 |
| `ASTRABOX_PUBLIC_PATH_PREFIX` | 管理台与其他应用共用一个主机时，网关发布管理台所用的路径，例如 `/astrabox`，见[在子路径下发布 AstraBox](#publish-under-a-path)。启用团队登录时，安装脚本写入的回调地址位于该路径之下。 |

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
`docker compose logs -f server` 和 `docker compose down`。启用团队登录时，这些命令也包含 Casdoor。

### 启用团队登录 {#turn-on-team-login}

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh \
  | ASTRABOX_INSTALL_TEAM_LOGIN=casdoor \
    ASTRABOX_CONSOLE_ORIGIN=https://astrabox.example.com \
    ASTRABOX_OIDC_ISSUER=https://login.example.com bash
```

安装脚本会把 SSO 叠加配置 `containers/compose.sso.yaml` 加入 `~/astrabox/containers/.env`
中的 `COMPOSE_FILE` 设置，因此服务本身以及在 `~/astrabox/containers` 中运行的每条
`docker compose` 命令都会和 AstraBox 一起运行 Casdoor。Casdoor 监听 `127.0.0.1:8087`。
提供两个公网 URL 时，安装脚本还会写入服务端需要的回调地址和 Host 允许列表；请按
[通过代理提供登录](team-login.md#put-the-login-flow-behind-a-proxy)把两个回环端口放到
HTTPS 代理之后。只在本机登录时，两个 URL 都不要设置。

安装完成时，安装脚本会显示已生成的密码保存在哪里：登录 AstraBox 使用的 `admin` 账号的密码，
以及 Casdoor 自己的管理员 `built-in/admin` 的密码。AstraBox 只接受内置 `astrabox` 组织的账号，
见[启用内置登录服务](team-login.md#bundled-login-service)。

再次运行安装脚本会保持团队登录开启。要关闭它，请设置 `ASTRABOX_INSTALL_TEAM_LOGIN=none`
后运行。已配置公网 URL 的安装会拒绝关闭，直到从 `containers/.env` 中删除这些 URL，
以免向其他计算机提供没有登录的控制台。

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
或 Docker named volume，各工作区使用该文件系统中的独立目录。使用内置 OpenSandbox
后端时，用户沙箱之外创建的 mergerfs 视图承载这些目录，OpenSandbox 使用标准 PVC 或
named volume 接口挂载这些视图。在交给用户之前，平台将视图绑定到新建或已有的工作区
目录。存储适配器选择底层介质，沙箱后端决定如何挂载；设置了该卷时，只要有一个已启用的
后端不能挂载持久工作区，启动就会被拒绝。

未设置或设为空值时，工作区文件保存在沙箱的临时文件系统中；替换沙箱可能丢失这些文件。
这项配置只影响工作区文件，原生 SessionStore 仍由平台数据库保存。无卷部署不会启动挂载
辅助程序，与 `ASTRABOX_WORKSPACE_MOUNTER_IMAGE` 的取值无关。

启用工作区存储时，默认也挂载 Assistant profile。承载 profile 的文件系统必须支持程序
运行中的 SQLite 与原子文件操作。设置 `ASTRABOX_ASSISTANT_PROFILE_ON_WORKSPACE_VOLUME=false`
可以只挂载用户工作区，把 profile 留在沙箱文件系统。共享挂载计划在分配前应用这个选择；
存储提供方接收最终计划，不自行过滤路径。原生与 profile 快照仍保存在平台数据库；
保存未获确认时仍保留原分配。

Hermes 快照保留完整 SessionDB 和支持的 profile 状态：记忆、用户 Skill、定时任务与执行
历史、项目与记忆存储、看板、验证证据和 A2A 记录。运行配置和凭证由平台提供。
profile 中的任意额外文件、缓存与厂商消息配对数据不在快照约定内；用户任务文件应放在工作区。

为已有 Assistant 更改这项设置前，通过正常保存/休眠流程确认原生和 profile 快照均已保存。
保存未确认的 Assistant 必须先在保留的原分配上完成恢复。保留原底层目录和恢复备份；
更改设置不会删除或导入它们。

连接已有 OpenSandbox 服务（`ASTRABOX_SANDBOX_OPENAPI_BASE_URL`）时，持久工作区要求该
服务使用 Kubernetes 运行时，见
[连接已有 OpenSandbox 服务](providers/opensandbox.md#connect-an-existing-opensandbox-service)。

### 配置工作区挂载辅助程序

`ASTRABOX_WORKSPACE_MOUNTER_IMAGE` 指定辅助程序镜像；未设置时使用本版本发布的
`ghcr.io/colton-z/astrabox-workspace-mounter:<version>`。修改辅助程序时，用
`make build-workspace-mounter-image` 构建并发布不可变镜像，再把该变量指向它。这是独立
的主机侧辅助程序，不是 Agent 运行平台额外的组件。

只有一个可运行沙箱的主机时，可以设置 `ASTRABOX_WORKSPACE_STORAGE_TOPOLOGY=local`，
使用本地 ext4 等文件系统。多个沙箱主机必须使用 `shared`，且每台候选主机都能访问同一
远端文件系统；不同本地磁盘上的同名目录不是共享存储。仅增加 API 副本不会改变这个要求。

`ASTRABOX_WORKSPACE_MOUNT_ROOT` 指定主机上的分配视图目录，默认值为
`/var/lib/astrabox/workspace-mounts`，应与工作区数据分开。每个辅助程序只接收自己的
视图目录，使用双向挂载传播。Docker 部署需先把所在主机挂载设为 shared，否则 Docker
会拒绝从非共享源传播 `rshared` 挂载。辅助程序要求 amd64、Linux 6.9+、root、`/dev/fuse`
和特权挂载权限；这些权限不会授予用户沙箱。它要求原生 mergerfs I/O passthrough 和兼容的
缓存模式，不会静默关闭 passthrough。

Kubernetes 中的平台身份需要管理辅助 Pod 及其 exec 接口、分配视图 PVC/PV，并能列出
Node；命名空间必须允许这些特权基础设施 Pod。底层 PVC 仍由部署方管理。Kubernetes
调度辅助 Pod，视图 PV 则把沙箱限定在同一节点。辅助程序消失后，已有 FUSE 挂载失效，
不能靠在运行中的沙箱下重启辅助程序恢复。Docker 视图数据卷是辅助程序视图目录的递归绑定，
让沙箱能够看到其中编号的 FUSE 子挂载；非递归绑定只会暴露空的主机目录，挂载检查会拒绝
这种状态。该绑定是从属（slave）副本：最后一个容器退出时 Docker 会卸载数据卷，共享副本
会把这次卸载传回主机，卸掉辅助程序的视图。Docker 只在容器按名称使用这类数据卷时才挂载
它，因此沙箱通过 Docker 的数据卷子路径挂载获得工作区，这也是部署要求 Docker Engine 26.0
的原因之一（见 [Docker 版本要求](#docker-requirements)）。

每个辅助程序从创建起就记录它服务的分配、沙箱后端和所属的 AstraBox 安装。过期监视器在
确认沙箱已不存在后释放视图；对于从未被沙箱使用的视图（创建失败或准备中途停止），如果
该安装没有任何沙箱携带这个分配，会在辅助程序创建十分钟后将其删除。仍被容器或 Pod 挂载
的视图不会被删除。每个已启用后端的挂载路径分别回收。辅助程序记录的沙箱后端未在本部署
启用时，视图会被保留并记录日志，因为没有已启用的后端能确认它的沙箱已经不存在。

### 存储介质必须支持的操作

AWS EFS 使用 [`aws_efs` 存储适配器](providers/aws-efs.md)，它检查实际的 EFS CSI
claim，OpenSandbox 后端的工作区挂载路径保持不变。

工作区是常规工作目录，不是文档存储，准备工作区的代码会用到以下操作。缺少其中任何一项的
存储介质都不能使用，无论它的容量多么方便：

| 操作 | 用途 |
|---|---|
| 覆盖已有路径的 `rename` | 逐个发布 Plugin 仓库缓存文件，避免其他对话读取未完成的版本 |
| `chmod`（保留 mode） | 仓库中以 `100755` 提交的 Plugin 脚本；缺少它时，对话运行时拿到的可执行文件既不能运行也不能 `chmod`，因为缓存属于 root |
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
所有沙箱创建路径（包括预备容量）都使用平台的固定视图挂载方案，底层文件系统由所选
存储适配器提供。AstraBox 只接受为本部署配置的存储适配器。

### 部署前检查

在 Kubernetes 中启用工作区持久化时，应准备名称与
`ASTRABOX_SANDBOX_WORKSPACE_VOLUME` 一致、已绑定且支持 `ReadWriteMany` 的
PersistentVolumeClaim。存储辅助程序挂载这个底层数据卷，OpenSandbox 挂载分配给沙箱的
已就绪视图数据卷。所配置的存储适配器无法确认所需挂载时，AstraBox 会拒绝运行准备。

## 连接消息平台

服务镜像已经包含固定版本的 Satori adapter 和内置连接程序。在 Agent 页面创建消息平台
Deployment 并填写 provider 表单；详情页会显示回调地址或连接状态，并提供对应的官方
开发者控制台链接。

单服务部署通过容器 loopback 使用内置连接程序。多副本部署可以在经过认证的 HTTPS
地址后运行一个共享连接程序：将 `ASTRABOX_CHANNEL_GATEWAY_BASE_URL` 设置为该地址，
并在 AstraBox 和连接程序上设置相同的 `ASTRABOX_CHANNEL_GATEWAY_TOKEN`。要求见
[仓库中的配置参考](https://github.com/Colton-z/AstraBox/blob/main/docs/configuration.md)。

## 运行 connector hub {#run-the-connector-hub}

Assistant 和 Agent 的 connector（Gmail、Notion 以及[连接办公应用](assistants.md#connectors)中的其他办公应用）通过 connector hub ContextForge 运行，基础安装不会启动它。在仓库克隆中，带上 hub 的密钥加入它的 Compose 叠加文件，并带上本部署的 Google OAuth web 客户端加入 Google 叠加文件（用于 Gmail、Google 日历和 Drive）：

```bash
export CONTEXTFORGE_JWT_SECRET=$(openssl rand -hex 32)
export CONTEXTFORGE_AUTH_ENCRYPTION_SECRET=$(openssl rand -hex 32)
export CONTEXTFORGE_ADMIN_EMAIL=connectors@your-domain.example
export CONTEXTFORGE_ADMIN_PASSWORD='<强密码>'
export ASTRABOX_CONTEXTFORGE_PUBLIC_URL=https://<你的-astrabox-地址>/connector-hub
export GOOGLE_OAUTH_CLIENT_ID=<client ID> GOOGLE_OAUTH_CLIENT_SECRET=<client secret>
scripts/compose.sh -f containers/compose.connectors.yaml \
  -f containers/compose.connectors-google.yaml up -d --build
```

第一个叠加文件启动 ContextForge（由 `containers/contextforge` 构建，即固定版本加上其中列出的补丁），并让 server 指向它。第二个为 Gmail、Google 日历和 Google Drive 各启动一个 google_workspace_mcp 实例；不加它时这三个 connector 不可用，其他 connector 照常工作。

- **浏览器** 只在控制台同源的 `/connector-hub/oauth/callback` 访问 hub，每个厂商在用户授权后把浏览器送回这里；在每个厂商的 OAuth 应用中把这个地址登记为回调地址。厂商只接受 HTTPS 地址，或 `localhost`。
- **沙箱** 不直接访问 hub。agent 通过 AstraBox server 的平台 MCP 路由调用 connector，server 以这段对话所代表的那个人的身份把请求转给 hub，所以沙箱里既没有 hub 的地址也没有任何人的 token，hub 也不需要接入沙箱网络。
- **限流** 在 hub 中关闭。ContextForge 按客户端统计请求，而它唯一的客户端是 AstraBox server，所以它的限额会一次性作用于整个部署：默认所有人合计每分钟 30 次 OAuth 请求和 100 次工具调用，多次超限后锁定 15 分钟。不使用这个叠加文件运行的 hub 也需要设置 `RATE_LIMITING_ENABLED=false`。
- **管理员邮箱** 的域名必须是 hub 接受的邮箱域名，不能是 `.local` 这类保留域名；每个 AstraBox 用户的 hub 账号都以这个域名命名。

hub 把每个人的厂商 token 保存在它自己的加密数据库中（`contextforge-data` 卷），请和平台数据一起备份。

## AstraBox 数据库 {#the-astrabox-database}

PostgreSQL 是支持的默认数据库，通过 `ASTRABOX_PERSISTENCE_PROVIDER=postgres` 选择。
内置提供方要求 `ASTRABOX_DB_URL` 为 `postgresql://` URL。仓库维护的 Compose 和单容器
部署运行 PostgreSQL，并用生成的凭证构造 URL；使用此提供方的其他部署指向自己的 PostgreSQL
服务。所有平台副本使用相同提供方与数据库。

外部提供方通过 `astrabox.providers.persistence` 注册，并校验自己的原生 URL。它们必须
提供兼容的 SQLAlchemy 引擎、原生冲突操作、结构初始化、错误分类和物理协调，包括绑定事务的
通知、运行时锁、监听与重新同步。注册成功或使用可复用的 MySQL 冲突操作，不代表后端完整。
参阅[关系型持久化提供方](providers/relational-persistence.md)。

使用内置 `postgres` 提供方时，服务端启动后、开始处理请求前更新结构。同时启动的副本
通过 PostgreSQL advisory lock 轮流执行，因此每项结构变更只运行一次。结构变更失败时，
服务端停止，数据库结构保持原样。较旧的版本无法在已被较新版本升级过的数据库上启动。

使用此提供方时，每个服务端进程为仓库查询最多保留 30 个池连接，为运行时连接与归档锁另保留
最多 30 个。独立连接池让持锁方能够查询仓库，不被其他持锁方阻塞。此外还有一个 PostgreSQL
通知连接与内置调度器的连接。请按全部副本设置连接上限。服务端会在连接上监听通知并
缓存 prepared statement，因此请直接连接 PostgreSQL，或通过 session 模式的连接池连接。

## 运行多个平台副本

[平台多副本部署指南](deploy-distributed.md)说明如何让多个 API 主机连接共享服务并使用
相同的签名密钥。请求可以到达任一 API 副本，沙箱由 Kubernetes 独立调度。同一时间只有
一个副本持有某个 Session 与其 Agent 程序的连接。接收到该 Session 消息或回答的副本会把它
记录下来，再由 PostgreSQL 通知告知持有连接的副本。该副本停止后，另一个副本接管这个
Session：正常关闭时在几秒内完成，否则在停止副本的 30 秒租约到期后完成。增加 API
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

Session 消息、Agent 程序报告的回复与输出，以及原生会话状态都保存在 AstraBox 数据库中。
配置工作区数据卷后，文件独立保存，终止或替换沙箱不会删除它们，替代沙箱会挂载原工作区。
没有工作区数据卷时，文件依赖原沙箱或保留的文件系统快照。OpenSandbox 普通暂停保留根文件
系统，不恢复进程内存；它不是卷上工作区文件或数据库原生会话状态的持久化机制。

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

## 在子路径下发布 AstraBox {#publish-under-a-path}

网关在一个主机上服务多个应用时，可以把 AstraBox 发布在某个路径下，例如
`https://example.com/astrabox/`。请在 AstraBox 服务端设置这个路径。已安装的部署写入
`~/astrabox/containers/.env`，再运行一次安装脚本：

```bash
printf '%s\n' "ASTRABOX_PUBLIC_PATH_PREFIX='/astrabox'" >> ~/astrabox/containers/.env
```

该值以斜杠开头，不带结尾斜杠。各段只能使用字母、数字、`.`、`_`、`~` 和 `-`。留空表示
把 AstraBox 发布在主机根路径。值不合法时，服务端会拒绝启动并给出该值。

AstraBox 按请求路径中前缀之后的部分匹配路由，因此网关要在请求到达 AstraBox 之前去掉
前缀。可以用以下两种方式之一：

| 网关 | 去掉前缀的方式 |
|---|---|
| 反向代理 | 转发前去掉前缀，见下面的示例。 |
| 在自身进程中运行 AstraBox 的 Python 应用 | 把 AstraBox 挂载到该前缀，见[将 AstraBox 挂载到子路径](embedding.md#mount-astrabox-under-a-path)。 |

使用 nginx 时，`proxy_pass` 中的 URI 部分会替换匹配到的前缀：

```nginx
location /astrabox/ {
    proxy_pass http://127.0.0.1:8088/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # 会话事件和终端输出通过长连接流返回。
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

使用 Caddy 时，`handle_path` 会去掉前缀：

```caddyfile
example.com {
    redir /astrabox /astrabox/ 308
    handle_path /astrabox/* {
        reverse_proxy 127.0.0.1:8088 {
            flush_interval -1
        }
    }
}
```

请求以其他方式到达时，AstraBox 会拒绝该请求，错误信息会指出这个设置：

| 错误 | 原因 |
|---|---|
| `PUBLIC_PATH_PREFIX_NOT_REMOVED`（404） | 请求仍带着前缀：代理原样转发了前缀，没有去掉它。此时管理台会提示无法加载。使用 nginx 时，`proxy_pass` 需要结尾的 `/`。 |
| `PUBLIC_PATH_PREFIX_MISMATCH`（409） | Python 应用把 AstraBox 挂载到了与 `ASTRABOX_PUBLIC_PATH_PREFIX` 不同的路径。 |

健康检查、就绪检查、安装脚本以及回调服务端的沙箱都直接访问 AstraBox，分别使用
`/healthz`、`/readyz` 和沙箱回调地址，不经过网关。

AstraBox 在前缀之下提供管理台页面和文件、API 以及事件流。它对外给出的地址也位于前缀
之下：登录回调、分享链接、文件下载、管理台显示的 MCP 服务地址、渠道回调地址，以及
连接工作应用后返回管理台的地址。启用团队登录时，请在身份提供方登记
`https://example.com/astrabox/api/v1/auth/callback` 作为管理台的回调地址，见
[设置团队登录](team-login.md)。登录 Cookie 的作用路径限定在该前缀下。

请先设置前缀，再创建消息平台 Deployment 和 connector。渠道 Deployment 会保留创建时的
回调地址，connector 会保留设置时的地址。修改前缀后，请重新创建渠道 Deployment。
请更新 `ASTRABOX_CONTEXTFORGE_PUBLIC_URL` 和每个厂商 OAuth 应用的回调地址，再重新设置
connector。启用团队登录时，还要更新身份提供方的回调地址和 `ASTRABOX_OIDC_REDIRECT_URL`，
并设置新的 `ASTRABOX_AUTH_SESSION_SECRET`。否则，为旧路径签发的登录 Cookie 会让浏览器
在退出后仍保持登录。

LiteLLM 的管理页面从主机根路径加载文件，AstraBox 无法在前缀下提供这些页面。
**集成服务**中不会列出内置网关的管理台。Agent 页面上的**管理远程 MCP 服务**和
**管理 Skill** 会提示这些页面无法在前缀下打开（`PUBLIC_PATH_PREFIX_UNSUPPORTED`）。
Agent 的 MCP 服务和 Skill 仍可在其页面上编辑。请在网关自己的地址打开它的管理台。

路径只区分地址，不区分信任。同一主机上的所有应用共用一个浏览器源（origin），其他应用
的脚本可以带着已登录用户的 Cookie 调用 AstraBox。如果主机上的其他应用不可信，请为
AstraBox 使用独立的主机名。

## 设置沙箱规格和资源上限 {#size-sandboxes-and-cap-what-they-use}

对话独占的沙箱和 Agent 共享的沙箱各有一套规格：

| 沙箱 | 上限 | 请求 | 设置方式 |
|---|---|---|---|
| 对话独占的沙箱 | 1 个 CPU、1 GiB | 250m CPU、1 GiB | `ASTRABOX_SANDBOX_CPU_LIMIT`、`ASTRABOX_SANDBOX_MEMORY_LIMIT`、`ASTRABOX_SANDBOX_CPU_REQUEST`、`ASTRABOX_SANDBOX_MEMORY_REQUEST` |
| Agent 共享的沙箱 | 4 个 CPU、4 GiB | 1 个 CPU、4 GiB | 前缀为 `ASTRABOX_SHARED_SANDBOX_` 的同名四个变量 |

上限是沙箱最多能用的资源。请求是 Kubernetes 调度器在节点上为它预留的资源；Docker 不看
请求。内存请求等于上限，节点不会承诺它没有的内存。CPU 请求低于上限，因为 CPU 繁忙时
沙箱只是变慢。沙箱采用创建时生效的规格，直到被替换前都保持不变。

共享沙箱按实际用量（而不是上限）计算空闲内存，空闲内存还能给新对话留出 640 MiB 时才接入
它。一个 Claude Code 对话从启动起会增长约 570 MiB，所以 4 GiB 的共享沙箱大约容纳五个对话。
共享沙箱越大，单位内存能容纳的对话越多，因为底层运行时只占用一次：8 GiB 的沙箱大约容纳
十二个。

每个沙箱旁边运行一个出网 sidecar，上限为 1 个 CPU 和 128 MiB。在 Docker 主机上由随附的
沙箱服务设置这个上限。在 Kubernetes 上，OpenSandbox 0.2.3 创建的 sidecar 不带资源设置，
请为沙箱所在的命名空间配置一个 LimitRange，让容器默认值与之相同：

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: astrabox-sandbox-sidecars
spec:
  limits:
    - type: Container
      default: {cpu: "1", memory: 128Mi}
      defaultRequest: {cpu: 50m, memory: 128Mi}
```

这个默认值作用于该命名空间中所有没有自行声明资源的容器，包括 OpenSandbox 的 execd 初始化
容器。

### 限制沙箱后端的总用量 {#cap-what-a-sandbox-backend-runs}

沙箱额度限制一个沙箱后端为整个部署运行的资源总量，所有平台副本共用：

| 变量 | 限制 |
|---|---|
| `ASTRABOX_SANDBOX_QUOTA_BOXES` | 沙箱数量 |
| `ASTRABOX_SANDBOX_QUOTA_MEMORY` | 沙箱及其 sidecar 的内存上限之和，例如 `24Gi` |
| `ASTRABOX_SANDBOX_QUOTA_CPU` | 沙箱及其 sidecar 的 CPU 上限之和 |
| `ASTRABOX_SANDBOX_QUOTA_CONCURRENT_CREATES` | 同时创建中的沙箱数量 |

后端为本部署运行的每个沙箱都计入额度：对话使用的沙箱、共享沙箱、已准备沙箱、创建中的
沙箱，以及后端尚未确认删除的沙箱。暂停的沙箱在 Docker 主机上计入（它仍占着内存），在
Kubernetes 上不计入（暂停会释放它的 Pod）；恢复时重新占用额度。沙箱的占用量在创建时确定，
之后修改规格不会改变运行中沙箱的占用量。

会让后端超出某项上限的沙箱不会被创建：需要它的对话立即结束，错误信息说明是哪项上限、当前
用量和上限值，例如 `sandbox backend 'opensandbox' has no room for another box: boxes 12 of 12
in use, and this box needs 1`。不会排队等待。以这种方式被拒绝的已准备沙箱会稍后重试，间隔
逐渐拉长。已有沙箱的对话不受影响。

变量不设置即没有上限，只有一个例外：在 Docker 主机上运行随附的沙箱服务时，内存上限默认为
主机内存减去 4 GiB（留给平台、它的存储服务和主机本身）。16 GiB 的主机上就是 12 GiB，大约
够十个独占沙箱的对话。8 GiB 及以下的主机上，这个上限不超过 4 GiB，放不下一个默认规格的共享
沙箱（4 GiB 加 sidecar），运行环境使用共享沙箱的 Agent 或 Assistant 都无法启动：请调低
`ASTRABOX_SHARED_SANDBOX_MEMORY_LIMIT` 及其请求，或调高 `ASTRABOX_SANDBOX_QUOTA_MEMORY`。把变量设为 `unlimited` 即取消该项上限。启动时会记录每项上限及其来源；
如果后端无法列出自己的沙箱，就无法统计用量，此时设置上限会导致启动失败。控制台的「沙箱」
页面会显示每项上限和当前用量。

## 为已准备沙箱规划容量 {#plan-capacity-for-prepared-sandboxes}

每个 Agent 会按它的[预备容量](authoring-agents.md#prewarming)保留相应数量的空闲已准备运行时，
使同样多的新对话可以同时在一秒内开始，而不必先启动沙箱。只要设置了
`ASTRABOX_AGENT_PREWARM_REDIS_URL`，新建 Agent 的预备容量就默认为 1；所有随附的 Compose
部署和[单容器部署](all-in-one.md)都会设置它，在这些部署上，全新安装时创建的两个 Agent
同样如此。只要预备容量大于 0、Agent 及其运行环境处于启用状态，这些运行时就一直保留；
对话取走一个空闲沙箱之后，AstraBox 会再准备一个补上，每个 Agent 同时最多准备八个。请按
各 Agent 的 `prepared_capacity` 之和规划部署容量：例如一个服务 1,000 名用户、预计会有 100
个对话同时开始的 Agent，预备容量需要 100，每个对话独立使用沙箱时就是 100 个空闲沙箱。已准备
沙箱计入[沙箱额度](#cap-what-a-sandbox-backend-runs)。Agent 的对话共用沙箱时，每个已准备的沙箱按内存上限放入尽可能多的席位，每个席位预留 640 MiB，
Agent 保留的空闲沙箱数是预备容量除以每个沙箱的席位数并向上取整。在沙箱租期
（`ASTRABOX_SANDBOX_LEASE_SECONDS`）内没有对话取走的席位会被丢弃，与空闲沙箱相同。把
`ASTRABOX_PREPARED_CAPACITY_LIMIT` 设为部署能承受的总和，AstraBox 会拒绝会超出它的 Agent
写入。不设置时，一个 Agent 的已准备沙箱可能占满整个沙箱额度，不给对话留下余地。不需要快速
开始首个对话的 Agent 可以把预备容量设为 0。

空闲的已准备沙箱占用多少资源，取决于 Agent 的运行环境使用的 Agent 程序：

| Agent 程序 | 一个空闲已准备沙箱的内存占用 |
|---|---|
| Claude Code | 约 0.5 GiB：在 Docker 主机上沙箱容器占用 490–536 MiB |
| pi | 约 0.45 GiB：在 Docker 主机上沙箱容器占用 450 MiB |
| DeepSeek Harness | 在 Kubernetes 上每个沙箱 Pod 占用 921–940 MiB 匿名内存 |
| 其他 Agent 程序（包括 Codex） | 在 Kubernetes 上每个沙箱 Pod 占用 390–650 MiB 匿名内存 |

在 Docker 主机上，每个沙箱的出网 sidecar 另外占用约 60 MiB，空闲沙箱的 CPU 占用为单核的
2–5%。

| 沙箱运行位置 | 这些资源如何计算 |
|---|---|
| 单台 Docker 主机 | Docker 不为空闲容器预留资源，主机内存承载的就是上表中的占用加上 sidecar。沙箱额度仍按完整规格计入每个已准备沙箱。 |
| Kubernetes 上的 OpenSandbox | 调度器在节点上为每个沙箱预留它的请求：对话独占的沙箱 250m CPU 和 1 GiB，共享沙箱 1 个 CPU 和 4 GiB，另加命名空间 LimitRange 给 sidecar 的请求。 |
| 已有的 OpenSandbox 服务 | 请求相同；是否真正预留取决于该服务的运行时，与上面两行相同。 |

上表是空闲的已准备沙箱占用的资源；工作时沙箱最多可以用到它的[上限](#size-sandboxes-and-cap-what-they-use)。
要为某个 Agent 关闭预热，在它的「运行设置」中把
「预备容量」设为 0，或通过 API 或 `astrabox.yaml` 文档设置 `"prepared_capacity": 0`。

部署多个平台副本时，每个 Agent 的已准备沙箱同一时刻只由一个副本负责补充，任何副本都能
把它交给对话。在同一台主机上重启的副本会立即恢复补充；副本永久停止时，另一个副本会在
约两分半钟内接手补充，期间对话照常取用已经准备好的沙箱。

## 检查健康状态和沙箱恢复

AstraBox 分别提供存活与就绪检查：

```text
GET /healthz
GET /readyz
```

服务开始排空并准备关闭后，`/readyz` 会返回 `503`。平台的终止宽限时间应大于 `ASTRABOX_SHUTDOWN_DRAIN_SECONDS`，让该副本上正在处理的请求有时间完成。副本停止期间，Agent 程序在沙箱中继续运行，由另一个副本接管它们的 Session。

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
