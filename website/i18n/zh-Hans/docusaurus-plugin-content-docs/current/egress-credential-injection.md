# 保护 Agent 使用的凭证

AstraBox 可以把模型服务、远程 MCP 服务和外部 API 的凭证保留在沙箱外。根据认证方式，
Agent 使用不透明占位符或不含凭证的请求；兼容沙箱的出站代理只会为目标地址和请求规则
都匹配的请求加入真实凭证。Agent 程序、终端、模型和提示词都不会收到保存值。

![受保护凭证在哪里加入请求](./img/egress-credential-injection.svg#inline)

## 哪些凭证可以留在沙箱外

| 凭证 | 使用方式 | 可用范围 |
|---|---|---|
| 模型服务凭证 | 只为发往 Agent 所用模型网关的请求加入凭证 | Agent 和 Assistant Session |
| 远程 MCP 服务凭证 | 为对应的远程 MCP 服务加入 Bearer Token、OAuth Token 或 API Key 请求头 | Agent 和 Assistant Session |
| HTTP Basic 凭证 | 为不含凭证的 HTTPS URL（包括私有 Git 仓库请求）添加认证，无需沙箱内 Token 或占位符 | Agent Session |
| 其他 API 的凭证 | Agent 程序收到不透明的环境变量值，出站代理在匹配的 HTTP 请求中替换它 | 支持受保护环境变量凭证的 Agent 程序 |

Assistant 可以使用远程 MCP 服务的已保存凭证，但不能分配包含外部 API 环境变量凭证的 Credential Vault（凭证库），因为长期运行的工作区无法安全接收针对单次对话生成的占位符。
Assistant 也不支持 HTTP Basic Vault 分配。不受支持的分配会被拒绝。

## 在控制台配置受保护凭证

1. 打开**管理台 → 凭证**，新建凭证库。
2. 选择**添加凭证**，选择凭证用途，然后填写服务地址或环境变量名以及密钥内容。
3. 在**已分配给**中选择可以使用该凭证库的 Agent 或 Assistant。

开始对话的用户看不到，也不需要选择凭证库；分配关系由管理员维护。凭证库详情还会显示当前部署如何传递模型服务凭证、远程 MCP 服务凭证和其他服务凭证。

密钥会加密保存，保存后只能写入，不能读取。创建、轮换、停用和分配方法见[管理服务凭证](credentials.md)。

## 受保护凭证如何传递

Agent 使用基于占位符的受保护凭证时：

1. AstraBox 读取凭证，并为当前沙箱创建新的不透明占位符。
2. Agent 程序收到占位符；沙箱出站代理收到真实值和匹配规则。
3. 出站代理检查请求的目标地址、方法、路径和允许替换的位置。
4. 符合规则的请求会获得真实值；不符合规则的请求不会获得凭证，也可能被 Environment 的网络规则阻止。

凭证 API 永远不会返回真实值，控制台也不会显示。轮换 MCP 凭证后，AstraBox 会在下一项
顶层 Agent 任务开始前刷新受保护凭证，Agent 使用原有占位符。出站环境变量和 HTTP Basic
凭证在准备或重连运行时解析，不会在已运行的环境中随每项任务刷新。

HTTP Basic 直接注入请求头。请在 Vault 中配置不含凭证的 HTTPS 目标 URL、用户名和只写
密码或 Token。AstraBox 会在下载 Skill 和 Plugin 的 Git 仓库前安装认证规则，预热也使用
同一路径。主机和路径匹配时，OpenSandbox 添加原生 Basic 认证请求头。

## 目标地址和请求限制

其他 API 的凭证必须配置至少一个允许访问的主机。仓库维护的 OpenSandbox 后端支持完整域名、可选的 80 或 443 端口，以及 `*.example.com` 这样的最左侧通配符；IP 地址、单段主机名和其他端口会被拒绝。

默认必须使用 HTTPS。只有凭证明示允许明文传输时，才能使用 80 端口的 HTTP。凭证可以限制为只替换请求头、请求体或两者，还可以通过方法和路径进一步缩小范围。

远程 MCP 服务使用受保护认证时，必须提供不含查询参数的 Streamable HTTP URL，并使用 HTTPS 443 端口；明确允许明文传输后也可以使用 HTTP 80 端口。带认证的 SSE 不受支持，因为它的 POST 地址会动态返回，无法预先限制。

## 网络访问单独配置

Agent 的 Environment（运行环境）仍通过 `limited` 或 `unrestricted` 网络模式控制普通出站访问。分配受保护凭证会把它的准确目标地址加入沙箱实际使用的网络规则，但不会修改已保存的 Environment，也不会允许其他无关地址。凭证的方法和路径规则会继续决定哪些匹配请求能够获得密钥。

远程 MCP 调用和其他沙箱流量都使用当前部署的网络出口。Environment 设置和出站 IP 地址见[网络访问](networking.md)。

## 部署要求

仓库维护的部署默认启用受保护凭证：

```bash
ASTRABOX_SANDBOX_CREDENTIAL_VAULT=true
ASTRABOX_SANDBOX_EGRESS_MODE=dns+nft
```

沙箱后端必须提供出站代理，并支持受保护凭证。仓库维护的 OpenSandbox 部署会通过标准
创建路径为冷启动和客户端池容量提供这些组件。它们在 Session 领取沙箱前就已存在，
不能等到 Session 第一次需要凭证时再安装。

如果已经启用凭证保护，但所选后端无法提供，AstraBox 会拒绝启动运行环境，不会把真实凭证放进沙箱。如果运维者明确关闭凭证保护，模型凭证会通过 Agent 程序的环境变量传递；已保存的远程 MCP 服务凭证和外部 API 环境变量凭证将不可用。

自定义部署和预备容量的要求见 [OpenSandbox 凭证保护](providers/opensandbox.md#credential-protection)。

## 验证正在运行的沙箱

1. 在**管理台 → 凭证**中打开已分配的凭证库，检查三种凭证传递方式。
2. 使用已分配的 Agent 启动 Session，并向允许的目标地址发起一次请求。
3. 打开**管理台 → 沙箱**，选择对应沙箱，查看**网络与凭证**。该面板直接显示沙箱报告的网络规则和生效凭证名称，永远不会显示保存值。

Environment 独立声明普通出站可达性。`unrestricted` 允许所有地址；`limited` 允许
其允许列表和平台已知地址。Environment 记录无需复制 Vault binding，也不会读取 Plugin
内部的 MCP 声明：

```json
{
  "networking": {
    "type": "limited",
    "allowed_hosts": ["registry.npmjs.org", "api.example.com"],
    "allow_mcp_servers": true
  }
}
```

平台会推导模型地址、AstraBox 回调地址和已声明的 Plugin Git 地址。
`allow_mcp_servers` 允许 Agent 直接声明的远程 MCP URL。Plugin 保持引擎原生且内容
不透明；管理员需要列出 Plugin 内部使用的地址。其余请求由 provider 阻止，附加的 Vault
binding 明确授权精确目标地址时除外。

AstraBox 凭证计划会保留目标地址、方法、路径、transport 和安全传输意图。provider 必须
准确实现该范围，或者在分配前拒绝；不能静默扩大凭证范围。分配该计划也会把 binding
地址加入沙箱的有效策略。这不是第二项管理员任务：把范围为 `api.example.com` 的凭证
分配给工作负载，就代表授权访问 `api.example.com`。方法和路径规则继续决定密钥的注入
位置；同一地址上的其他请求不带凭证。

Vault 分配保持 Environment 记录原样。Environment 网络配置与 Vault 记录生命周期相互
独立。移除分配会从新沙箱策略中移除它管理的地址授权。

OpenSandbox egress v1.1.5 支持在 HTTPS 443 上拦截凭证；凭证明示允许不安全传输时也支持
HTTP 80，并且要求 `dns+nft`。它还会校验每个运行时 binding 地址明确可达。因此 adapter
会把每个已附加的 binding 地址渲染为精确允许规则。该规则在 `unrestricted` 下是冗余项；
在 `limited` 下则是 Vault 分配携带的地址授权。Environment 允许列表无需重复填写。

## 添加出站凭证

`environment_variable` 凭证可以让 Agent 访问指定的外部 API，同时把保存值
留在工作负载之外。先在 AstraBox Vault 中创建凭证：

```http
POST /api/v1/admin/vaults/{vault_id}/credentials
Content-Type: application/json

{
  "display_name": "GitHub API token",
  "auth": {
    "type": "environment_variable",
    "secret_name": "GITHUB_TOKEN",
    "secret_value": "ghp_…",
    "networking": {
      "type": "limited",
      "allowed_hosts": ["api.github.com"]
    },
    "injection_location": {
      "header": true,
      "body": false
    },
    "allowed_requests": {
      "methods": ["GET"],
      "paths": ["/repos/acme/*"]
    }
  }
}
```

把 Vault 分配给 Agent：

```http
PUT /api/v1/admin/agents/{agent_id}/credential-vaults
Content-Type: application/json

{"vault_ids": ["<vault_id>"]}
```

Session 会在 `GITHUB_TOKEN` 中收到不透明值。主机、方法、路径和注入位置都匹配时，
所选 provider 才会替换保存值。更换沙箱也会更换占位符。

MCP 凭证类型可以分配给 Agent 和 Assistant。引擎直接连接已配置的 MCP URL；AstraBox
把 provider 网关请求头和匹配的 Vault 请求头合并为 provider-neutral、绑定目标地址的
计划。使用支持受保护传递的沙箱后端时，Agent 可以使用出站环境变量凭证。
通用运行准备流程会为每次分配提供独立的占位符上下文。

## 限定凭证替换范围

| 字段 | 支持的范围 |
|---|---|
| `networking.type` | `limited` 或 `unrestricted` |
| `allowed_hosts` | 精确主机、可选端口，或 `*.example.com` 这样的最左侧通配符 |
| 目标地址 | HTTPS；设置 `allow_insecure_http: true` 后可使用 HTTP |
| `injection_location` | 请求头、请求体或两者 |
| `allowed_requests.methods` | 一个或多个 HTTP 方法 |
| `allowed_requests.paths` | `/repos/acme/*` 这样的绝对路径模式 |

省略 `allowed_requests` 时只应用主机范围。更新凭证会轮换只写入的保存值；归档凭证
会移除保存的密钥并保留元数据。下一个根 turn 的准备阶段会把活动 MCP 绑定替换成同一
目标地址和 Vault 范围内的免凭证绑定。

provider-neutral MCP 凭证计划会保留配置的 URL 和 transport。OpenSandbox 当前要求带凭证
的端点不含查询参数，并使用 HTTPS 443 或 HTTP 80。它的 adapter 会拒绝带凭证 SSE，
因为 OpenSandbox 无法预先绑定该 transport 动态返回的 POST URL；能保持这项范围的其他
provider 可以支持。免凭证 SSE 可以使用。

## 使用 OpenSandbox 准备容量

预热沙箱在 AstraBox 分配时已经存在。SDK 客户端池的 creator 会通过与冷启动容量相同的
标准 OpenSandbox 创建路径生成每个成员，在任何 Session 出现前提供有效网络策略并启用
凭证代理。OpenSandbox 会配置代理，为该沙箱的端点生成认证信息，并把它返回给 SDK。
池只保留沙箱 ID；领取时通过 OpenSandbox 重新连接，并取回该沙箱自己的认证信息。
这里不存在部署级出站令牌。

AstraBox 在发布 Agent 预备沙箱前，会用非密钥探测值验证所需 binding。真实凭证只在
分配或恢复沙箱后写入；Environment 允许列表无需重复 Vault binding 地址。

自定义部署和预备容量的要求见
[OpenSandbox 凭证保护](providers/opensandbox.md#credential-protection)。

## 验证凭证交付

在**管理 → Credential Vault**中确认显示的交付模式和 Vault 分配。然后新建 Session，
测试一个符合主机、方法和路径规则的请求。访问其他地址时出现 DNS 解析错误，说明
Environment 允许列表已经生效。

完整 Vault 请求和响应模型以 `/docs` 或 `/openapi.json` 当前发布的字段定义为准。

如果沙箱无法报告安全设置，控制台会明确显示错误，不会把空响应当作凭证已经受到保护的证据。

## 相关指南

- [管理服务凭证](credentials.md)
- [网络访问](networking.md)
- [OpenSandbox](providers/opensandbox.md)
- [连接模型服务](models.md)
