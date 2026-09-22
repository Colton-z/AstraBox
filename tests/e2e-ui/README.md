# e2e-ui — Playwright UI suite

这套测试使用真实浏览器、真实后端和真实沙箱。文件按并发方式分为两类：
`*.parallel.spec.ts` 属于并行 lane；`*.exclusive.spec.ts` 属于隔离要求更高的 lane。
维护的 runner 按冻结清单把 exclusive lane 分成普通并行组和单 worker 的重启/破坏性组；
文件后缀本身不会让直接调用的 Playwright 自动串行。

真实 Slack、飞书和钉钉账号不属于普通 CI 的可复现依赖，因此
`real-slack-feishu-dingtalk.external.spec.ts` 只有在显式设置
`ASTRABOX_E2E_EXTERNAL_CHANNELS=1` 时才会被收集。它没有 mock、skip 或凭据 fallback：
每个平台都必须提供一个绝对路径、非符号链接、权限为 `0600` 的 JSON 文件：

```json
{
  "channel_config": {"appId": "visible application id"},
  "credentials": {"appSecret": "write-only application secret"}
}
```

Slack 文件使用空的 `channel_config` 以及 `token`、`botToken`；飞书使用 `appId`、
`appSecret`；钉钉使用 `appkey`、可选 `agentId` 和 `secret`。分别通过
`ASTRABOX_E2E_SLACK_CHANNEL_FILE`、`ASTRABOX_E2E_FEISHU_CHANNEL_FILE`、
`ASTRABOX_E2E_DINGTALK_CHANNEL_FILE` 传入。逐平台运行时使用精确标题：

```bash
ASTRABOX_E2E_EXTERNAL_CHANNELS=1 \
ASTRABOX_E2E_SLACK_CHANNEL_FILE=/absolute/private/slack.json \
npx playwright test specs/real-slack-feishu-dingtalk.external.spec.ts \
  --grep 'real Slack account carries one Agent turn end to end'
```

用例建立真实绑定后会输出 `EXTERNAL_CHANNEL_READY` 和一次性 marker。此时从正常用户账号
向测试机器人发送该 marker；Playwright 会继续验证持久化入站、Agent Session、助手消息、
平台返回的出站消息 id，以及控制台/API 都没有回显机器人密钥。机器人自己发消息不算
验收，因为渠道层会把机器人事件按防回环规则忽略。

## 运行

针对一个已经在运行的部署直接调用 Playwright，用完整文件路径和用例标题选例：

```bash
cd tests/e2e-ui
ASTRABOX_E2E_BASE_URL=http://<host>:<server-port> \
npx playwright test 'specs/postgresql-persistence.parallel.spec.ts' \
  --grep 'API writes are durable in the real PostgreSQL document store'
```

单个用例通过不等于 lane 通过：完整验收需要覆盖当前清单的全部引擎和两个存储分区，
并且不接受 skip、重试或超时。首红即停，失败现场（沙箱、日志、部署状态）保留下来再排查。

`run-round.mjs` 是按 `.e2e/spec-ledger.json` 排序的直接运行工具，会包含已通过文件的
回归阶段。它拒绝 `--reporter` 和 `--config`，以保留 JSON 结果与单项 180 秒预算 reporter。

## 环境契约

- `ASTRABOX_E2E_BASE_URL`：测试进程访问 AstraBox 的地址。在 AWS 主机上使用
  对应部署的回环端口。沙箱回调走独立的 `sandbox-edge`，不会复用这个地址，也不会
  因此获得整个 Docker 网关的访问权限。
- **故障注入规格**(`turn-settles-*`)要求 fault 文件
  路径在**测试进程与 server 容器间共享**:server 由 compose 注入
  `ASTRABOX_E2E_FAULTS=1` 且读 `/shared/e2e-faults/…`(把宿主机目录挂载到容器的
  `/shared`);测试侧
  必须显式设 `ASTRABOX_E2E_*_FAULT_FILE` 指向同一 host 路径,否则规格写
  `/tmp` 默认路径、后端读不到,断言以
  "fault must be consumed by the real backend turn worker" 失败。
- 部分 background 规格带模型行为 probe。未出现被测行为不等于验收通过；维护的
  campaign 遇到 `skipped` 会停止并判定该轮失败。保留实际输入、原生事件和报告后定位原因，
  不通过跳过或重试获得通过记录。

### expose-port 端点形状

`ASTRABOX_E2E_ENDPOINT_KIND` 声明被测部署配置的 OpenSandbox 端点形状；spec 不会
从 URL 猜部署模式。未设置时默认 `docker`。支持值与部署契约如下：

| 值 | 对应部署 | URL 形状与验证意图 |
|---|---|---|
| `docker` | 单机 Docker runtime | execd 的原生 `/proxy/<port>` 路径 |
| `direct` | Kubernetes runtime，OpenSandbox `[ingress] mode = "direct"` | 可从测试浏览器路由到的 Pod 地址，根路径 `/`；页面、相对 CSS 与 WebSocket 验证主机实际可达且内容来自被测 sandbox |
| `gateway-uri` | Kubernetes runtime，`[ingress] mode = "gateway"` 且 `gateway.route.mode = "uri"` | 非根路由路径；启用 Secure Access 时同一路径还携带签名段 |

`direct` 是 OpenSandbox 明确配置的一等模式，不是 ingress 组件缺失后的降级。它只适合
AstraBox 和浏览器都能路由到 Pod CIDR 的可信部署。Secure Access 要求 gateway；把它与
`direct` 同时配置会被 bundled lifecycle server 在启动时拒绝。

## 恢复系列 fixtures

- `dbOracle.ts` 通过部署服务句柄查询 PostgreSQL 的
  `astrabox_documents` 表，并提供
  `patchSessionDoc`/`patchSnapshotDoc`/`replaceDocs`/`restoreDoc` 等故障模拟工具。
  非 Compose 部署必须设置 `ASTRABOX_E2E_POSTGRES_CONTAINER`；Compose 部署未设置时
  会按 service label 发现唯一的运行中 `postgres`。发现不到唯一服务时规格会带配置
  方法显式 skip；维护的 campaign 会把这个 skip 视为失败。显式配置的容器不存在或未运行
  则 hard fail。
  超级用户探针可用 `ASTRABOX_E2E_POSTGRES_SUPERUSER` 显式声明登录名；未设置时从
  已选中的容器执行 `printenv POSTGRES_USER` 读取部署值，不假定角色名为 `postgres`。
  容器未公开该值或读取失败时会 hard fail，并说明如何显式配置。
- `sandboxOps.ts` 有 `restartServerContainer(baseUrl)`(`docker restart` + 双等 healthcheck 与 base URL 可达),供 serverless 实例替换类规格用。
- **server-restart 类规格 opt-in**:`ASTRABOX_E2E_ENABLE_SERVER_RESTART=1`(重启共享 server 容器会打断同 box 其他 spec)。Node `fetch` 用 `env.ts` 的 `absoluteApiUrl`/`absoluteBaseUrl`(`apiPath` 只在 Playwright request 上下文内相对解析)。

需要直接操作部署服务的规格统一使用容器句柄。句柄是测试宿主机 Docker daemon 可见的
容器名或 ID；没有显式句柄时只为本地 Compose 提供按 service label 的自动发现，不会
启动、停止或删除部署服务。Casdoor/PostgreSQL 隔离规格在非 Compose 部署上的跑法是：

```bash
cd tests/e2e-ui
ASTRABOX_E2E_CASDOOR_CONTAINER=<running-casdoor-container> \
ASTRABOX_E2E_OIDC_ISSUER=https://<casdoor-origin> \
ASTRABOX_E2E_POSTGRES_CONTAINER=<running-postgres-container> \
ASTRABOX_E2E_LITELLM_CONTAINER=<running-litellm-container> \
ASTRABOX_E2E_LITELLM_POSTGRES_CONTAINER=<running-litellm-postgres-container> \
npx playwright test specs/postgresql-runtime-services.exclusive.spec.ts
```

## 部署 fixtures

### OpenSandbox Agent 预热

`opensandbox-agent-prewarm-real-extensions.exclusive.spec.ts` 需要两个预先配置好的
Environment（`agent` 与 `conversation` tenancy）、Redis、PostgreSQL，以及能正常调用
模型的 Agent 镜像。它不会创建或修改 Environment；这些是可跨轮复用的部署 fixture。

部署时通过管理 API（`PUT /api/v1/admin/environments/{name}`）幂等创建并读回专用的
`astrabox-e2e-prewarm-shared` 与 `astrabox-e2e-prewarm-conversation`。前者使用
`agent` tenancy，后者使用 `conversation` tenancy；两者都使用本轮部署的 Agent 镜像。
不要复用面向日常用户的 `claude-code` Environment，也不要从经过脱敏的管理 API 响应
克隆凭证字段。两个 Environment 与 namespace 必须显式传给 spec；缺一项时长跑前即失败。

```bash
ASTRABOX_E2E_BASE_URL=http://<aws-host>:<server-port> \
ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT=astrabox-e2e-prewarm-shared \
ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT=astrabox-e2e-prewarm-conversation \
ASTRABOX_E2E_POSTGRES_CONTAINER=<running-postgres-container> \
npx playwright test specs/opensandbox-agent-prewarm-real-extensions.exclusive.spec.ts
```

两种 tenancy 都使用 OpenSandbox SDK client pool。AstraBox 在池发布沙箱前完成
Skills/Plugins 等准备；`sandbox_tenancy=agent` 还在共享沙箱内准备隔离的引擎运行位置，
`sandbox_tenancy=conversation` 则准备完整沙箱供用户会话领取。两条路径都从 Agent 的
`prepared-runtime` 状态读取是否就绪和当前准备版本，不读取 Kubernetes 供应商内部对象。

spec 只读取两个 Environment。每个 case 会创建自己的 Agent 与 Sessions，
conversation case 还会更新一次 Agent 版本；通过时按新到旧删除 Sessions，再 soft-delete
Agent 并退役其 client pool 或准备箱，失败时则保留这些对象和沙箱供排查。部署
Environment 始终保留。

### Credential Vault 请求范围

`credential-request-matching.exclusive.spec.ts` 会创建真实的 Vault、Agent 和 Session，
并分别验证普通新建和 Agent 预热沙箱。终端命令和 Agent 的 Bash 工具都会访问同一个
测试服务：允许的 `GET /allowed` 必须成功，`POST /allowed` 和 `GET /denied` 必须因
凭证没有被添加而返回 `401`；允许请求的精确成功码是 `204`。同一个真实 Agent turn
还会通过团队 HTTPS 网关调用
`GET /v1/models`，并确认 Agent 工具进程只能读到模型凭证占位值。浏览器、终端输出和
会话流中都不能出现真实凭证。

`scripts/k8s-testbed.sh up` 会在 sandbox namespace 幂等安装 HTTP probe。设置团队网关
URL 时，它还会安装一个只暴露 443 的 Caddy `LoadBalancer` Service，把通过公开 FQDN
进入的 TLS 流量反代到
`astrabox-model-gw.<namespace>.svc.cluster.local:80`。先让该 FQDN 的 DNS 指向测试床
节点或 LoadBalancer 地址，并开放入站 TCP 443，然后在执行 `up` 的环境中导出 URL：

```bash
export ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL=https://llm.example.com
scripts/k8s-testbed.sh up
```

Caddy 通过 ACME 为该公开域名取得系统信任的证书；这里不能用自签证书，也不能把
Service 443 直接转发到 LiteLLM 的明文 80。spec 会从真实 Agent 工具进程执行
`curl https://.../v1/models`，而 OpenSandbox Credential Vault 也拒绝关闭上游 TLS
校验。`fixtures/secure-team-gateway.Caddyfile` 是脚本挂载的最小 TLS 终止配置；生产部署
仍应使用组织维护的负载均衡器或 Ingress。

部署 AstraBox 时，`ASTRABOX_LITELLM_BASE_URL` 必须使用同一个 HTTPS origin，使模型
credential binding 与 spec 都命中 443 网关；server 侧模型枚举仍可通过
`ASTRABOX_LITELLM_SERVER_BASE_URL` 使用集群内地址。如果 Environment 使用预热池，
脚本的默认拒绝规则会从 probe URL 和 HTTPS gateway URL 推导两个完整域名。若显式设置
`EGRESS_RULES`，它仍是完整覆盖，必须自行包含 probe、网关、回调和其他所有目的地。

维护的 AWS release worker 会预置两个可跨轮复用的专用 Environment：cold case 使用
`sandbox_tenancy=conversation`；Agent-prewarm case 使用 `sandbox_tenancy=agent`。
两者都 pin 到本次 release 的 Agent image，
使用部署的 LiteLLM endpoint/credential（不复制 secret），并在长 lane 前 read-back
验证。直接绕过 release worker 运行 spec 时，下面四个值仍必须由部署证据显式提供；
spec 不读取 ambient fallback，也不会临时改写共享 Environment。

```bash
ASTRABOX_E2E_BASE_URL=http://<aws-host>:<server-port> \
ASTRABOX_E2E_CREDENTIAL_PROBE_URL=http://astrabox-e2e-credential-request-probe.<namespace>.svc.cluster.local \
ASTRABOX_E2E_CREDENTIAL_PREWARM_ENVIRONMENT=<agent-prewarm-environment> \
ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT=<cold-conversation-environment> \
ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL=https://llm.example.com \
npx playwright test specs/credential-request-matching.exclusive.spec.ts
```

## 当前覆盖与失败证据

用例归属和数量见
[suite-contract.json](../../tests/e2e-contract/suite-contract.json)。递归子任务树由
`nested-child-runs-preserve-tree.exclusive.spec.ts` 覆盖；其他行为按清单选择具体用例。
失败状态以 verifier 返回的原始报告为准，不从文档中的历史问题列表推断。
