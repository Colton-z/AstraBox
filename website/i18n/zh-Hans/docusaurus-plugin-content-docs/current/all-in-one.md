# 单容器运行 AstraBox

> 用一条 `docker run` 命令启动单用户的 AstraBox。

all-in-one 镜像在一个容器里运行 AstraBox、它的 PostgreSQL 数据库、内置模型网关和沙箱
生命周期服务，所有数据都保存在一个 Docker 卷中。这是在 Linux 机器上试用 AstraBox 最简单的
方式。团队使用、共享服务器或需要团队登录时，请改用[快速入门的安装脚本](quickstart.md)，
它用 Docker Compose 运行同样的服务。

每个 Session 仍然有自己的沙箱容器。all-in-one 容器通过 Docker socket 在 Docker 主机上
创建它们，同时创建两个小型代理容器和两个网络，见[容器旁边运行的对象](#beside-the-container)。

## 前置条件

- 一台以 root 方式运行 Docker Engine 的 Linux 主机。容器启动时会拒绝 rootless Docker 和
  Podman。Docker Desktop 尚未与此镜像一起验证。
- 约 20 GB 可用磁盘空间存放镜像。首次启动会下载 Claude Code 沙箱镜像：下载约 4 GB，解压后约 14 GB。
- 空闲时约 2.8 GiB 内存（在 x86-64 主机上测得）：AstraBox 容器约 1.6 GiB；AstraBox 会为它创建的两个
  Agent 各保留一个已准备的沙箱，让它们的第一个对话不必等待沙箱启动，每个沙箱连同出口代理约 0.55 GiB。
  每个开启预热的 Agent 都会保留一个已准备的沙箱，沙箱工作时最多可增长到 4 GiB。见
  [为已准备沙箱规划容量](deploy.md#plan-capacity-for-prepared-sandboxes)，其中也说明了如何为某个 Agent 关闭预热。
- 一个模型服务的 API Key：Anthropic、DeepSeek，或其他 Anthropic 兼容、OpenAI 兼容的服务。

## 第 1 步：写入模型设置

把模型服务的设置写进一个只有你能读的文件。Docker 会把它们传给容器，也不会留在 shell
历史记录里。

使用 Anthropic 时：

```bash
mkdir -p ~/.config/astrabox
cat > ~/.config/astrabox/model.env <<'EOF'
ANTHROPIC_API_KEY=your-anthropic-api-key
ANTHROPIC_MODEL=your-model-id
EOF
chmod 600 ~/.config/astrabox/model.env
```

使用其他服务时，改写为以下内容：

| 服务 | `model.env` 中的内容 |
|---|---|
| DeepSeek | `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`、`ANTHROPIC_API_KEY=` 你的 DeepSeek Key、`ANTHROPIC_MODEL=deepseek-flash` |
| Anthropic 兼容服务 | `ANTHROPIC_BASE_URL=` 服务地址、`ANTHROPIC_API_KEY=` 它的 Key、`ANTHROPIC_MODEL=` 它的模型 ID |
| OpenAI 兼容服务 | `OPENAI_COMPATIBLE_BASE_URL=` 服务地址、`OPENAI_COMPATIBLE_API_KEY=` 它的 Key、`ANTHROPIC_MODEL=openai-compatible/` 加上它的模型 ID |

`ANTHROPIC_MODEL` 是预置 Agent 使用的模型。这些设置与安装脚本写入的相同；
[连接模型](models.md)说明了它们背后的网关路由。如果暂时不接模型服务，第 2 步去掉
`--env-file` 这一行，之后在**管理台 → 集成服务 → LiteLLM 网关**中添加路由。

:::note
如需使用运行在同一主机上的模型服务，请在第 2 步的命令中加上
`--add-host host.docker.internal:host-gateway`，并以 `host.docker.internal` 作为它的主机名。
:::

## 第 2 步：启动 AstraBox

运行：

```bash
docker run -d --name astrabox --restart unless-stopped \
  -p 127.0.0.1:8088:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v astrabox-data:/data \
  --env-file ~/.config/astrabox/model.env \
  ghcr.io/colton-z/astrabox:0.1.1
```

然后查看日志：

```bash
docker logs -f astrabox
```

首次启动会初始化数据库并下载 Claude Code 沙箱镜像，需要几分钟，日志中会报告进度。镜像下载
到本机之后 AstraBox 才开放控制台，因此第一个 Session 不必等待下载。日志出现下面这一行后，
在浏览器中打开该地址：

```text
AstraBox is ready at http://127.0.0.1:8088
```

接着按[快速入门的第 2 步](quickstart.md)继续：选择运行环境、创建 Agent 并开始 Session。

:::warning
这种部署方式的控制台没有登录，并且只发布在本机回环地址上。不要把它发布到其他地址。需要
共享 AstraBox 时，请使用[快速入门的安装脚本](quickstart.md)并配置[团队登录](team-login.md)
和 TLS。
:::

容器只在启动的短暂时间内以 root 运行，用于让服务账号获得 Docker socket 和数据卷的访问权限，
之后所有服务都以该账号运行。不要添加 `--user`、`--group-add`、`--hostname` 或
`--network host`：后两个会被容器拒绝，因为它通过 Docker socket 按容器 ID 找到自己。

## 容器旁边运行的对象 {#beside-the-container}

OpenSandbox 为沙箱设置的网络策略只按主机匹配、不按端口匹配，因此绝不能允许沙箱访问
AstraBox 容器本身。容器启动时会在 Docker 主机上创建下列对象，名称取自保存在数据卷中的
安装标识：

| Docker 对象 | 用途 |
|---|---|
| `astrabox-<id>-sandbox-edge` | 沙箱唯一可以访问的 AstraBox 地址，只转发模型网关和 Agent 的平台回调。 |
| `astrabox-<id>-sandbox-dns-edge` | 沙箱解析模型网关私有名称时使用的 DNS 服务。 |
| `astrabox-<id>-sandbox-edges` | 连接两个代理与 AstraBox 的内部网络，沙箱不会加入。 |
| `astrabox-<id>-platform` | AstraBox 自己的网络。容器启动时从 Docker 默认网桥移到这个网络，主机上的其他容器因此无法访问它的端口。控制台仍发布在 `127.0.0.1:8088`。 |
| 每个 Session 一个沙箱容器，以及每个开启预热的 Agent 一个已准备的沙箱 | 由 Agent 的镜像创建，旁边有一个出口代理。 |

数据库和其他内部服务只在 AstraBox 容器内部监听。沙箱无法访问它们，也无法访问其他沙箱，
除了经过代理以外无法访问 AstraBox 的任何端口。

AstraBox 重启时代理继续运行，已经在运行的沙箱会保持连接。代理崩溃时由 Docker 重启它。
如果它回来时换了地址，AstraBox 也会重启；仍在使用旧地址的沙箱所属的对话会换到新沙箱上继续，
历史记录保留。

## 升级

数据、密钥和设置都在 `astrabox-data` 卷中，所以升级就是用新版本运行同一条命令：

```bash
docker pull ghcr.io/colton-z/astrabox:<new-version>
docker stop astrabox && docker rm astrabox
docker run -d --name astrabox --restart unless-stopped \
  -p 127.0.0.1:8088:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v astrabox-data:/data \
  --env-file ~/.config/astrabox/model.env \
  ghcr.io/colton-z/astrabox:<new-version>
```

请使用 `docker stop`，不要用 `docker rm -f`：正常停止才能让数据库干净地关闭。启动时
AstraBox 会更新数据库，并在开放服务之前下载新版本的沙箱镜像。你的 Session 及其历史
会保留下来。

由其他 PostgreSQL 主版本写入的卷会被拒绝启动，并给出说明。更换主版本的发布版本会说明
如何迁移数据。

## 备份与恢复

卷中保存了所有内容，包括解密你所保存凭证的密钥。请像保管密钥本身一样保管备份。先停止
AstraBox，保证备份一致：

```bash
docker stop astrabox
docker run --rm -v astrabox-data:/data -v "$PWD":/backup --entrypoint tar \
  ghcr.io/colton-z/astrabox:0.1.1 -czf /backup/astrabox-data.tgz -C /data .
docker start astrabox
```

恢复时，把备份解压到一个新卷：

```bash
docker volume create astrabox-data-restored
docker run --rm -v astrabox-data-restored:/data -v "$PWD":/backup --entrypoint tar \
  ghcr.io/colton-z/astrabox:0.1.1 -xzf /backup/astrabox-data.tgz -C /data
```

然后删除旧容器，用 `-v astrabox-data-restored:/data` 运行第 2 步的命令。恢复出的副本
要替代原来的安装，不能同时运行：两者属于同一个安装，后启动的一个会停止并给出说明。

同一时间只能有一个容器使用一个卷。在同一个卷上启动第二个容器时，它会停止并给出说明，
第一个容器继续运行。

## 卸载

以下命令删除 AstraBox、它创建的代理和网络、它的沙箱及沙箱卷，以及它的全部数据：

```bash
docker stop astrabox && docker rm astrabox
docker ps -aq --filter label=astrabox.all-in-one.install | xargs -r docker rm -f
docker network ls -q --filter label=astrabox.all-in-one.install | xargs -r docker network rm
docker ps -aq --filter label=opensandbox.io/id | xargs -r docker rm -f
docker ps -aq --filter label=opensandbox.io/egress-sidecar-for | xargs -r docker rm -f
docker volume ls -q --filter label=opensandbox.io/volume-managed-by | xargs -r docker volume rm
docker volume rm astrabox-data
```

需要删除沙箱的几行，是因为结束沙箱的租约由 AstraBox 容器维护：它被删除后，就没有任何
东西会停止它的沙箱。这几行会删除 Docker 主机上所有 OpenSandbox 沙箱和沙箱卷，包括该主机
上其他 AstraBox 安装的。

## all-in-one 容器不支持的功能

容器运行一种固定的配置。启动时，它会拒绝改变这种配置的设置，逐项列出并说明应改用什么。
以下场景需要 [Compose 安装](deploy.md)：

- **团队登录以及从其他电脑访问**。请使用 Compose，并配置[团队登录](team-login.md)和 TLS。
- **外部数据库、Redis 或模型网关**。容器运行自己的这些服务。
- **多台机器**，或在 Kubernetes 上运行沙箱。参见[分布式部署](deploy-distributed.md)。
- **持久化工作区卷**以及**关闭 Credential Vault**。
- **把 all-in-one 安装迁移到 Compose**。目前还没有受支持的迁移步骤。

每个 Docker 主机只运行一个 all-in-one 容器。

## 常见问题

**问：容器刚启动就停止了，怎么回事？**

答：运行 `docker logs astrabox`。以 `FATAL` 开头的一行会说明停止的原因，例如被拒绝的设置、
容器无法打开的 Docker socket，或正被另一个容器使用的卷。

**问：日志一直停在下载沙箱镜像。**

答：Claude Code 沙箱镜像约 4 GB，日志每 15 秒报告一次进度。如果下载失败，容器会带着镜像
仓库返回的错误停止；Docker 重启它时会再次尝试。

**问：可以使用镜像仓库的镜像源吗？**

答：在 `model.env` 中把 `ASTRABOX_IMAGE_PREFIX` 设为镜像源的前缀，例如
`registry.example.com/astrabox-`，沙箱镜像就会从该镜像源拉取。容器拉取镜像时不带你的
仓库登录信息；如果镜像源需要登录，请先在主机上用 `docker pull` 拉取沙箱镜像。
