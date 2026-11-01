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
| 其他 API 的凭证，[请求头投递](#header-delivery) | Agent 程序收到不透明的环境变量值，沙箱后端只在一个指定的请求头中、为所列域名替换它 | 使用支持请求头投递的沙箱后端的 Agent Session |

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
4. 符合规则的请求会获得真实值；不符合规则的请求不会获得凭证，也可能被 RuntimeProfile 的网络规则阻止。

凭证 API 永远不会返回真实值，控制台也不会显示。轮换 MCP 凭证后，AstraBox 会在把下一条
消息交给 Agent 程序之前刷新受保护凭证，Agent 使用原有占位符。出站环境变量和 HTTP Basic
凭证在运行准备或重连时解析，运行期间不会在每条消息前重新解析。重连时，沙箱收到之后又被停用
或删除的这类凭证也会从沙箱中移除。重连后的运行时保留其进程启动时的占位符，从预备容量启动的
进程也是如此。

HTTP Basic 则直接注入请求头。请在 Vault 中配置不含凭证的目标 URL、用户名和只写的密码或
Token；沙箱内的请求不含凭证占位符。AstraBox 会在下载 Skill 和 Plugin 的 Git 仓库前安装
凭证规则，预备容量也是如此。HTTPS 主机和路径匹配时，OpenSandbox 添加原生 Basic 认证
请求头。参见[私有 Git 示例](credentials.md#2-add-a-credential)。

## 目标地址和请求限制

其他 API 的凭证必须配置至少一个允许访问的主机。仓库维护的 OpenSandbox 后端支持完整域名、可选的 80 或 443 端口，以及 `*.example.com` 这样的最左侧通配符；IP 地址、单段主机名和其他端口会被拒绝。

默认必须使用 HTTPS。只有凭证明示允许明文传输时，才能使用 80 端口的 HTTP。凭证可以限制为只替换请求头、请求体或两者，还可以通过方法和路径进一步缩小范围。

远程 MCP 服务使用受保护认证时，必须提供不含查询参数的 Streamable HTTP URL，并使用 HTTPS 443 端口；明确允许明文传输后也可以使用 HTTP 80 端口。带认证的 SSE 不受支持，因为它的 POST 地址会动态返回，无法预先限制。

HTTP Basic 要求使用 443 端口、路径不是根路径的不含凭证 HTTPS URL。它匹配该路径本身及其子路径上的 `GET`、`HEAD` 和 `POST` 请求。例如，`https://github.com/acme/private-skills.git` 覆盖 `/acme/private-skills.git/info/refs`，但不覆盖 `/acme/private-skills.git-other/info/refs`。不匹配的请求不会从这项绑定获得 Basic 凭证。重定向后的目标地址要保持在配置的范围内；AstraBox 不会把凭证转发给其他主机或代码仓库路径。

## 网络访问单独配置

Agent 的 RuntimeProfile（运行配置）仍通过 `limited` 或 `unrestricted` 网络模式控制普通出站访问。分配受保护凭证会把它的准确目标地址加入沙箱实际使用的网络规则，但不会修改已保存的 RuntimeProfile，也不会允许其他无关地址。凭证的方法和路径规则决定哪些匹配请求能够获得密钥。

远程 MCP 调用和其他沙箱流量都使用当前部署的网络出口。RuntimeProfile 设置和出站 IP 地址见[网络访问](networking.md)。

## 部署要求

仓库维护的部署默认启用受保护凭证：

```bash
ASTRABOX_SANDBOX_CREDENTIAL_VAULT=true
ASTRABOX_SANDBOX_EGRESS_MODE=dns+nft
```

沙箱后端必须提供出站代理，并支持受保护凭证。仓库维护的 OpenSandbox 部署会通过标准
创建路径为冷启动和客户端池容量提供这些组件。它们在 Session 领取沙箱前就已存在，
不会等到 Session 第一次需要凭证时才安装。

如果已经启用凭证保护，但所选后端无法提供，AstraBox 会拒绝启动运行时，不会把真实凭证放进沙箱。

Agent 程序收到的是不含密钥的占位符。沙箱创建后，AstraBox 把真实的网关凭证及其匹配规则
写入出站代理。所选 Agent 程序的集成会把该凭证限定在它的模型 API 方法和路径上。

需要明确通过沙箱环境变量传递凭证时，设置 `ASTRABOX_SANDBOX_CREDENTIAL_VAULT=false`。
这种模式下，所选 Agent 程序通过进程环境变量收到模型或网关凭证。已保存的远程 MCP 服务
凭证、HTTP Basic 凭证和替换投递的环境变量凭证在这种模式下不可用，因为它们的值不能进入
工作负载。使用[请求头投递](#header-delivery)的环境变量凭证在支持它的沙箱后端上仍然可用。
设置开启期间收到过受保护凭证的沙箱，在设置关闭后不会被重新接入，因为已无机制管理或撤销这些
凭证；它所在的对话需要换用新的沙箱。

管理台会显示当前生效的传递模式，返回的凭证元数据不含保存值。

## 独立配置普通网络访问

RuntimeProfile 独立于凭证传递声明普通出站可达性。`unrestricted` 允许所有地址；`limited`
允许其允许列表和平台已知地址，它不会重复 Vault binding，也不会读取 Plugin 内部的 MCP
声明：

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
`allow_mcp_servers` 允许 Agent 直接声明的远程 MCP URL。从扩展目录分配的服务通过部署的
扩展网关访问，网关地址无需这个设置即可访问。Plugin 保持 Agent 程序的原生格式，AstraBox 不解析其内容；管理员
需要列出 Plugin 内部使用的地址。发往其他主机的请求都会被阻止，除非附加的 Vault binding
明确授权了该精确目标地址。

AstraBox 凭证计划会保留目标地址、方法、路径、transport 和安全传输意图。provider 必须
准确实现该范围，或者在分配前拒绝；不能静默扩大凭证范围。分配该计划也会把 binding
地址加入沙箱的有效策略。这不是第二项管理员任务：把范围为 `api.example.com` 的凭证
分配给工作负载，就代表授权访问 `api.example.com`。方法和路径规则仍然决定哪些请求会被注入
密钥；同一主机上的其他请求不带凭证。

Vault 分配保持 RuntimeProfile 记录原样。RuntimeProfile 网络配置与 Vault 记录生命周期相互
独立。移除分配会从新沙箱策略中移除它管理的地址授权。

OpenSandbox egress v1.1.7 支持在 HTTPS 443 上拦截凭证；凭证明示允许不安全传输时也支持
HTTP 80，并且要求 `dns+nft`。它还会校验每个运行时 binding 地址明确可达。因此 adapter
会把每个已附加的 binding 地址渲染为精确允许规则。该规则在 `unrestricted` 下是冗余项；
在 `limited` 下则是 Vault 分配携带的地址授权。RuntimeProfile 允许列表无需重复填写。

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
所选 provider 才会代入保存的值。更换沙箱也会更换占位符。

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

省略 `allowed_requests` 时只应用主机范围。更新凭证会轮换只写入的保存值；停用凭证
会移除保存的密钥并保留元数据，并且 AstraBox 会在 Agent 程序收到下一条消息之前，把活动
MCP 绑定替换成同一目标地址和 Vault 范围内的免凭证绑定。

provider-neutral MCP 凭证计划会保留配置的 URL 和 transport。OpenSandbox 当前要求带凭证
传递的端点是不含查询参数的 Streamable HTTP 端点，并使用 HTTPS 443 或 HTTP 80。它的
adapter 会拒绝带凭证 SSE，因为 OpenSandbox 无法预先绑定该 transport 动态返回的 POST URL；
能保持这项范围的其他 provider 可以支持。免凭证 SSE 可以使用。

## 在一个请求头中投递凭证 {#header-delivery}

有些沙箱后端不在出站代理中替换占位符，而是在一个指定的请求头中替换它。创建
`environment_variable` 凭证时设置 `delivery: "header"`，就会使用这种方式：

```http
POST /api/v1/admin/vaults/{vault_id}/credentials
Content-Type: application/json

{
  "display_name": "Issue tracker API key",
  "auth": {
    "type": "environment_variable",
    "secret_name": "TRACKER_API_KEY",
    "secret_value": "tk_…",
    "delivery": "header",
    "header_name": "X-Api-Key",
    "networking": {
      "type": "limited",
      "allowed_hosts": ["api.tracker.example", "*.tracker.example"]
    }
  }
}
```

Agent 程序在 `TRACKER_API_KEY` 中收到不透明的占位符。它在发往所列域名的请求中把这个
占位符放进 `X-Api-Key` 请求头时，沙箱后端会把它替换成保存的值。除此之外不作任何承诺：

- 不替换请求体、路径或查询参数。
- 不检查请求方法、路径、端口或协议。
- 不匹配的请求会原样发出，其中仍是占位符。与替换投递不同，请求头投递不会拒绝这样的请求。

`header_name` 是 HTTP 请求头字段名。`networking` 必须是 `limited`，每一项都是完整域名，
可以以 `*.` 开头；端口、协议、路径、IP 地址和单段主机名会被拒绝。`injection_location`、
`allowed_requests` 和 `allow_insecure_http` 不适用，出现时会被拒绝。`delivery` 在创建凭证时
确定；要换用另一种方式投递密钥，请新建凭证。

请求头投递只取决于沙箱后端：支持它的后端自己完成替换，所以
`ASTRABOX_SANDBOX_CREDENTIAL_VAULT` 不会启用或停用它。只有已启用的后端支持请求头投递时，
才能创建这种凭证，否则会以 `VAULT_ENV_CREDENTIAL_UNSUPPORTED` 拒绝；控制台也只在这种
情况下显示**投递方式**选项。所选沙箱后端不支持时，Session 会在分配沙箱前被拒绝。仓库维护的
OpenSandbox 后端不支持请求头投递。两种投递方式的环境变量凭证都不能分配给 Assistant。

## 使用 OpenSandbox 预备容量

预热沙箱在 AstraBox 领取它时已经存在。SDK 客户端池的 creator 会通过与冷启动容量相同的
标准 OpenSandbox 创建路径生成每个成员，在任何 Session 出现前提供有效网络策略并启用
凭证代理。OpenSandbox 会配置代理，为该沙箱的端点生成认证信息，并把它返回给 SDK。
池只保留沙箱 ID；领取时通过 OpenSandbox 重新连接，并取回该沙箱自己的认证信息。
这里不存在部署级出站令牌。

在准备流程下载 Skill 或 Plugin 之前，creator 会把 Agent 当前完整的凭证计划提供给出站代理
一侧的 Vault。模型、环境变量、MCP 和 HTTP Basic 凭证使用与冷创建相同的传递路径；预热
不会用虚假的密钥值代替。受保护的值留在沙箱环境之外。分配和恢复通过同一份计划刷新当前
绑定。RuntimeProfile 允许列表无需重复 Vault binding 地址。

自定义部署和预备容量的要求见
[OpenSandbox 凭证保护](providers/opensandbox.md#credential-protection)。

## 验证正在运行的沙箱

1. 在**管理台 → 凭证**中打开已分配的凭证库，检查三种凭证传递方式。
2. 使用已分配的 Agent 启动 Session，并向允许的目标地址发起一次请求。
3. 打开**管理台 → 沙箱**，选择对应沙箱，查看**网络与凭证**。该面板直接显示沙箱报告的网络规则和生效凭证名称，永远不会显示保存值。

如果沙箱无法报告安全设置，控制台会明确显示错误，不会把空响应当作凭证已经受到保护的证据。

## 相关指南

- [管理服务凭证](credentials.md)
- [网络访问](networking.md)
- [OpenSandbox](providers/opensandbox.md)
- [连接模型服务](models.md)
