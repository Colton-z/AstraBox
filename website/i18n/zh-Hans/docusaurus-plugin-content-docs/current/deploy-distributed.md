# 共享服务的平台多副本部署

`containers/compose.distributed.yaml` 为已有的 Kubernetes 部署添加平台副本。每台主机
运行一个 AstraBox API 进程，副本共用 PostgreSQL 数据库、OpenSandbox 客户端池 Redis、
OpenSandbox 生命周期 API、LiteLLM 和消息网关。沙箱由 Kubernetes 调度，AstraBox 不为
每个用户请求指定机器。

这个叠加配置负责连接应用与共享服务，不创建这些服务，不迁移已有部署、不安装负载
均衡器，也不会让单个服务自动获得高可用能力。

适用于使用显式共享签名密钥的新部署，或已经使用相同显式密钥的部署扩容。从自动派生
回调密钥切换过来需要维护和凭证轮换计划，不是无感的配置迁移。

## 准备共享服务

- 使用同一个 AstraBox 数据库，包括 SessionStore 和加密的 Vault 记录。所有 API 主机
  必须能通过私网访问 PostgreSQL 和 Redis；只绑定首台主机 loopback 的端口无法被另一台
  访问。仅允许参与部署的主机连接，不向公网开放。
- 使用一个经过认证的外部 OpenSandbox 服务，连接同一个 Kubernetes 集群和命名空间。
  所有 API 主机都必须能访问生命周期接口及其返回的沙箱端点。
- 叠加配置会替换基础 Compose 的环境变量，因此要把已有工作区配置写入环境文件。持久化
  工作区使用 `ASTRABOX_STORAGE_PROVIDER`、`ASTRABOX_SANDBOX_WORKSPACE_VOLUME` 和
  `ASTRABOX_WORKSPACE_MOUNTER_IMAGE`。AWS EFS 使用 `ASTRABOX_STORAGE_PROVIDER=aws_efs`
  和相同的 `ASTRABOX_EFS_FILE_SYSTEM_ID`，并安装官方 CSI driver。叠加配置设置了
  `ASTRABOX_WORKSPACE_STORAGE_TOPOLOGY=shared`，所以底层 claim 必须是所有候选沙箱节点
  都能挂载的共享文件系统。详见[工作区存储](deploy.md#where-conversation-workspaces-live)
  和 [AWS EFS 工作区存储](providers/aws-efs.md)。工作区持久化是可选能力。
- 为 API 主机提供容器 uid 999 可读的 kubeconfig，叠加配置要求提供该文件。即使生命周期
  服务在外部，平台工作区挂载控制器和 EFS 适配器仍使用 Kubernetes。集群、命名空间须与
  OpenSandbox 一致。kubeconfig 对应的身份需要具备
  [工作区存储](deploy.md#where-conversation-workspaces-live)中列出的辅助程序权限；
  EFS 适配器还会读取底层 claim 及其 PersistentVolume。
- 共用 HTTPS LiteLLM 服务及其现有推理、管理凭证。共用经过认证的 HTTPS 消息网关及其
  token；分别启动内置网关不等于共享消息运行时。
- 在副本前部署由部署方管理、支持 HTTP、WebSocket 和 SSE 的统一入口。所有副本使用
  相同公网 origin、身份配置和 Pod 可访问的回调 URL。将公网、回调及私网健康检查主机名加入
  `ASTRABOX_ALLOWED_HOSTS`。下线副本前先排空，终止宽限时间应大于
  `ASTRABOX_SHUTDOWN_DRAIN_SECONDS`。

### OpenSandbox 版本边界

使用 OpenSandbox Server 0.2.3 时，应运行一个外部生命周期服务，并使用它自己的持久
SQLite 存储。这支持多个 AstraBox 副本和沙箱节点，但**不提供生命周期控制面的高可用**。不要
运行多个各自使用 SQLite 的生命周期副本，并认为共享 Kubernetes 就能共享快照记录。
[0.2.3 配置参考](https://github.com/opensandbox-group/OpenSandbox/blob/c39b814/server/configuration.md#store)
仅支持 SQLite 管理服务端元数据。采用其他上游版本时，应根据其共享存储保证另行验收。

内置生命周期进程监听容器 loopback，单独发布主机端口不会使其可被外部访问。外部服务
应遵循 OpenSandbox 官方服务配置，包括 API key 鉴权。迁移生命周期服务时，保留持久
元数据，停止旧写入者后再交接存储；不要复制正在写入的 SQLite 数据库并同时运行两个写入者。

## 配置每个副本

在各主机上创建仅运维人员可读的绝对路径环境文件，如 `/etc/astrabox/replica.env`，
同时用于 Compose 变量替换和容器环境注入。不要提交该文件或公开 `docker compose config`
的渲染结果，两者都含有凭证。

共享部分使用以下已有应用配置：

```dotenv
ASTRABOX_DB_URL=postgresql+asyncpg://astrabox:URL_ENCODED_PASSWORD@db.internal:5432/astrabox
ASTRABOX_AGENT_PREWARM_REDIS_URL=redis://redis.internal:6379/0
ASTRABOX_SANDBOX_OPENAPI_BASE_URL=https://sandbox-control.example.com
ASTRABOX_SANDBOX_API_KEY_SECRET_NAME=opensandbox-api-key
OPENSANDBOX_API_KEY=REPLACE_WITH_SHARED_LIFECYCLE_API_KEY
ASTRABOX_SANDBOX_SERVER_KUBE_API_SERVER=https://kubernetes.internal:6443
ASTRABOX_SANDBOX_SERVER_KUBE_NAMESPACE=opensandbox
ASTRABOX_MCP_PROXY_BASE_URL=https://callbacks.example.com
ASTRABOX_ALLOWED_HOSTS=astrabox.example.com,callbacks.example.com,api-a.internal,api-b.internal
ASTRABOX_LITELLM_BASE_URL=https://llm.example.com
ASTRABOX_CHANNEL_GATEWAY_BASE_URL=https://channels.example.com
ASTRABOX_CHANNEL_GATEWAY_TOKEN=REPLACE_WITH_EXISTING_SHARED_GATEWAY_TOKEN
ASTRABOX_VAULT_MASTER_KEY=REPLACE_WITH_EXISTING_SHARED_VAULT_KEY
ASTRABOX_AUTH_SESSION_SECRET=REPLACE_WITH_EXISTING_SHARED_SESSION_KEY
ASTRABOX_TRANSCRIPT_SIGNING_KEY=REPLACE_WITH_EXPLICIT_SHARED_TRANSCRIPT_KEY
```

生命周期 API key 引用由 `astrabox/secrets.py` 的 secret provider 解析，不读取产品的
加密 Vault 记录。当前 provider 只读进程环境，将名称大写并把连字符转为下划线，所以
`opensandbox-api-key` 对应 `OPENSANDBOX_API_KEY`。每个副本都要提供相同的值；仅共享
产品数据库不会使该密钥自动可用。环境文件可以携带部署方选择的名称，无须新增专属配置项。

已有部署使用的其他配置也都要写入这个文件。保留现有 LiteLLM 配置，包括已有部署设置的
`ASTRABOX_LITELLM_API_KEY`、`ASTRABOX_LITELLM_SERVER_BASE_URL` 和
`ASTRABOX_LITELLM_ADMIN_URL`。`ASTRABOX_LITELLM_API_KEY` 为空时，每个副本的入口程序
使用 `LITELLM_MASTER_KEY` 确认或创建由 `ASTRABOX_AUTH_SESSION_SECRET` 派生的沙箱模型
密钥；缺少该值时副本不会启动。同时保留 `ASTRABOX_WEB_IDENTITY` 等身份配置和已注册的
OIDC 配置；这个叠加配置不负责设置身份提供商，也不会安装第二个 Casdoor。

叠加配置选择由共享数据库保存记录的 local secret-store provider，加密密钥必须沿用
现有部署，不能重新生成。`/data/vault.key` 中的编码值可以直接用于
`ASTRABOX_VAULT_MASTER_KEY`，保持 Vault 加密不变；`/data/auth-session.key` 可以用于
`ASTRABOX_AUTH_SESSION_SECRET`，保持浏览器会话签名不变。

回调签名不同：未显式配置 transcript key 时，从文件读取 Vault key 和从环境变量读取
同一个编码值会使用不同的回调密钥派生方式。因此，把 Vault 值复制到环境变量不会保留
已有回调凭证。显式 transcript 配置接收文本密钥，不导入任意二进制派生密钥；不要把
派生结果 base64 编码后当成等价配置。

没有显式 transcript key 的已有部署需要协调轮换：停止接收新任务、排空在途任务、退役
携带旧回调凭证的预热及常驻运行时，在所有副本配置同一个显式 transcript key，再准备
替代运行时。保留产品数据库、SessionStore 和工作区存储，在新凭证验收通过后恢复流量。
叠加配置不会自动轮换，也不会更新已有沙箱的 token。不要整体复制 `/data`；每个副本
使用独立本地数据卷，不复制数据库。

在同一个文件中加入各主机自己的 Compose 参数：

```dotenv
ASTRABOX_REPLICA_ENV_FILE=/etc/astrabox/replica.env
ASTRABOX_SERVER_IMAGE=registry.example.com/astrabox/server@sha256:REPLACE_WITH_IMAGE_DIGEST
ASTRABOX_SERVER_BIND_IP=10.0.1.12
ASTRABOX_SERVER_HOST_PORT=8088
ASTRABOX_KUBECONFIG_HOST_PATH=/etc/astrabox/kubeconfig
ASTRABOX_STATE_VOLUME=astrabox-api-b-state
```

每台主机使用相同的不可变服务镜像和沙箱镜像，只有绑定地址、本地路径和本地数据卷名称
不同。`ASTRABOX_REPLICA_ENV_FILE` 是 Compose 文件路径，不是应用配置。使用绝对路径
可避免 Compose 按基础文件目录解析路径带来的歧义。容器固定监听 `0.0.0.0:8000`，主机
发布端口只通过 `ASTRABOX_SERVER_BIND_IP` 和 `ASTRABOX_SERVER_HOST_PORT` 调整。

## 仅启动平台副本

在各主机的仓库根目录运行：

```bash
docker compose --env-file /etc/astrabox/replica.env \
  -p astrabox-api-b \
  -f containers/compose.yaml \
  -f containers/compose.kubernetes.yaml \
  -f containers/compose.distributed.yaml config --quiet

docker compose --env-file /etc/astrabox/replica.env \
  -p astrabox-api-b \
  -f containers/compose.yaml \
  -f containers/compose.kubernetes.yaml \
  -f containers/compose.distributed.yaml up -d --no-deps --no-build server
```

`!override` 合并标签需要 Docker Compose 2.24.4 或更新版本，分布式叠加配置必须放在最后。
不要通过 `scripts/compose.sh` 启动这套拓扑，它会准备本地数据库凭证。不要启用
`local-data-only` 或 `docker-runtime-only` profile。叠加配置移除了本地服务依赖，不挂载
Docker socket 或本地数据库密钥。入口程序识别外部服务 URL 后，不会启动内置的
OpenSandbox、LiteLLM 或消息网关进程。

先通过各副本私网地址检查 `/healthz` 和 `/readyz`，再将健康副本加入统一入口。部署验证
应覆盖两个副本并发领取、沙箱唯一分配、任一副本访问同一 Session 的历史和消息流，以及
跨副本停止和审批操作。工作区在不同沙箱节点上的持久化应单独验证。这些检查覆盖真实的
共享服务连接，配置渲染或健康检查无法代替。
