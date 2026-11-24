# Assistant

Assistant 是只属于一个用户的个人云端工作区。多次对话共享同一个工作区，Agent 程序的
原生状态由 AstraBox 保存到平台数据库。沙箱释放或丢失后需要保留工作区文件时，部署方
应配置可选的持久工作区卷。

持续整理笔记、分析不断变化的文件，或分几天完成同一个项目，都适合使用 Assistant。需要
让用户或其他系统通过管理台、API、Deployment 或远程 MCP 使用可复用的云端 Agent 时，
应创建 [Agent](authoring-agents.md)。

## Agent 与 Assistant

| | Agent | Assistant |
|---|---|---|
| 谁可以使用 | 通过 Agent 鉴权的账户 | 归属用户 |
| 典型用途 | 重复执行的任务、团队使用和自动化 | 一个人持续进行的工作 |
| 工作区 | 每次对话使用独立工作区 | 归属用户的多次对话使用同一工作区 |
| 启动方式 | 管理台、API、Deployment 或远程 MCP | 管理台、API，或由归属用户管理的 Deployment |

Assistant 不会把多次对话合并为一段对话。每次对话都有独立的 Session 记录和历史，但这些
Session 会在同一个 Assistant 工作区中运行。

## 你的 Assistant

每个用户无需任何设置就有一个**主 Assistant**。用户第一次打开控制台时，AstraBox 按部署的
[Assistant 默认配置](#defaults)为其创建，时区取浏览器的[时区](#时区)，并列在左侧栏顶部。
创建时也会尝试准备工作区，让第一段对话可以直接接入。准备失败时，已经保存的 Assistant
仍会保留，第一段对话会再次尝试准备。在此之前已经有 Assistant 的用户，
其最早的那个会被标为主 Assistant。

主 Assistant 需要一个 **RuntimeProfile（运行配置）**：一套保存好的运行设置，包括 Agent 程序、
沙箱设置、模型连接和网络访问。AstraBox 使用 Assistant 默认配置指定的 RuntimeProfile；
默认配置没有指定时，使用唯一一个已启用且 Agent 程序支持 Assistant 的 RuntimeProfile。
如果这样的 RuntimeProfile 有多个而默认配置没有选择，或者一个都没有，用户就不会有 Assistant，
左侧栏也不显示 Assistant 分区；此时需要管理员在 Assistant 默认配置中选定 RuntimeProfile。

创建主 Assistant 不需要创作 Agent 与 Assistant 的权限，所以即使部署只允许管理员创作，
每个用户也都有一个。用户已经连接的应用会为它开启，与新建任何 Assistant 时一样。

### 更多 Assistant

Assistant 创建页面只显示已启用且支持 Assistant 的 RuntimeProfile。

1. 选择左侧栏 **Assistant** 旁的 **+**，或打开**管理台 → Assistant**，选择**创建 Assistant**。
2. 填写名称并选择 **RuntimeProfile**。
3. 根据需要填写说明和[系统提示词](#系统提示词)，并选择默认[权限模式](permission-modes.md)。
   [时区](#时区)默认取浏览器的时区。
4. 选择**创建**。

当前登录用户会成为归属用户。RuntimeProfile 同时决定 Assistant 使用的 Agent 程序；创建后，
管理台不会再提供这两项的修改入口。名称、说明、图标、时区、系统提示词和默认权限模式仍可编辑。

保存修改后的定义时，会尝试原地刷新已就绪的工作区；刷新失败时，下一段对话会准备当前版本。
已有的就绪对话也会在使用配置前检查版本。仅仅创建或编辑了记录，并不代表工作区已经就绪。

## Assistant 默认配置 {#defaults}

Assistant 能做什么，由运行部署的人和它的归属用户决定。管理员在
**管理台 → Assistant 默认配置**（或 `/api/v1/admin/assistant-defaults` 及其 `/extensions`）
设置每个 Assistant 都会继承的内容：

- 新建主 Assistant 使用的 **RuntimeProfile**，或自动选择；
- Assistant 自己没有指定时使用的**模型**；
- **组织指令**，在归属用户自己的[系统提示词](#系统提示词)之前交给每个 Assistant；
- 每个 Assistant 都有的 **Skill、MCP 服务、软件包和 Agent 程序配置**，可以直接填写，
  部署启用[扩展目录](adding-tools.md#extension-catalog)时也可以从目录中选择；
- 绑定到每个 Assistant 的 **Credential Vault**。被默认配置绑定的 Vault 会列在它的凭证页上，
  在默认配置解除绑定之前不能归档或删除。

这些内容按 Assistant 自身字段的同一套规则校验：所选 RuntimeProfile 的 Agent 程序读取什么、
能访问什么，所以要先选定 RuntimeProfile 才能填写扩展字段。Assistant 会把默认配置和自己的
设置一起读取，因此修改的生效时机与[修改何时生效](#修改何时生效)所述相同。

与 Assistant 自身设置的合并方式：指令在前，归属用户的在后；Skill、软件包和 Vault 叠加；
MCP 服务叠加，Assistant 自己的 MCP 服务不能与继承的同名；Assistant 设置的某个 Agent 程序
配置块会替换继承的同名配置块。默认配置指定的 RuntimeProfile 在默认配置改指别处之前不能删除。

归属用户可以在 Assistant 的资料页和第一段对话中看到继承的内容，但只能编辑自己的设置。

## 系统提示词

系统提示词定义 Assistant 的角色、表达方式和行为规则。它与 [Agent](authoring-agents.md)
的系统提示词是同一项设置，由 Agent 程序按自己的机制使用。

内置的 Assistant 程序 Hermes 把它作为自己的 `SOUL.md`，也就是系统提示词开头的身份设定。
系统提示词会替换 Hermes 内置的“You are Hermes Agent”身份，Hermes 自身提示词的其余部分
（例如工具使用说明）保持不变。Hermes 会像检查任何 `SOUL.md` 一样检查这段文字：命中其
提示词注入规则的内容不会被载入，超过其上下文文件长度上限（至少 20,000 个字符）的内容会被截断。

留空则保留 Hermes 自身的默认身份。清除已设置的系统提示词会恢复这一默认身份；例外是
AstraBox 写入之后又被修改过的 `SOUL.md`（例如通过 Hermes 做的修改），这一版本会被保留。
设置了系统提示词时，AstraBox 会在 Hermes 启动或 Assistant 设置变更时重新写入它，覆盖这类修改。

## 时区

Assistant 按属主的时区计时，时区是 IANA 名称，例如 `Asia/Shanghai`（API 中为
`timezone`）。Hermes 会在每个对话的系统提示词里写明它，并按这个时区解读自己创建的定时
任务，所以“每个工作日 8 点”会在属主当地早上 8 点执行。未设置时，Hermes 使用沙箱的时钟，
即 UTC。

## 技能、插件与 Hermes 配置

Assistant 可以给它的 Agent 程序添加该程序接受的内容。Assistant 的记录页有一张
**技能、插件与配置**卡片，只显示 RuntimeProfile 的程序声明支持的字段；API 使用与
[Agent](authoring-agents.md) 相同的字段：`skills`、`mcp_servers`、`packages` 和
`engine_options`。部署启用[扩展目录](adding-tools.md#extension-catalog)时，同一张卡片也可以像 Agent 页面一样从目录中选择 MCP 服务器和 Skill（`GET`/`PUT /api/v1/assistants/{assistant_id}/extensions`）；需要上游凭证的目录 MCP 服务器，通过分配给该 Assistant 的 Credential Vault 访问。不启用目录时，卡片中只有 Assistant 自己的字段。对 Hermes 而言：

- **技能。** `SKILL.md` 技能的 Git 来源，写作 `仓库@引用#路径`。AstraBox 把它们放进
  配置目录的 `skills/astrabox` 分类，Hermes 会列出它们，并用 `skill_view` 加载。技能名
  不能与配置目录中的其他技能重名：Hermes 发现同名技能时会拒绝加载。
- **扩展包。** 每一项注明它的 `kind` 和 Git 来源（`url`、`protocol`、`branch` 或固定的
  `sha`，以及仓库中扩展包目录的 `paths`）。Hermes 加载两种：
  - `hermes-plugin`：带 `plugin.yaml` 的目录，可以添加工具、钩子、斜杠命令、技能和
    系统提示片段。Hermes 的包管理器将插件声明的 Python 依赖与 Hermes 自身的依赖一起
    准备；安装失败会停止对话准备。网络为“受限”时，会在与扩展包 Git 来源相同的授权边界内
    放行 PyPI（`pypi.org`、`files.pythonhosted.org`）及 Hermes 固定依赖的 GitHub 下载主机
    （`github.com`、`release-assets.githubusercontent.com`）。
  - `agent-plugin`：Agent Plugins v1.0 包，即带技能和 `mcp.json` 的 `plugin.json`。

  扩展包目录会成为插件 `astrabox/<目录名>`，目录名取自它的路径；位于仓库根目录的
  扩展包取仓库名。AstraBox 只启用它放置的插件，并在它们离开配置时移除；Assistant 自己
  安装的插件和技能保持不变。
- **Hermes 配置**（`engine_options.config`）。Hermes 的 `config.yaml` 键，例如
  `timezone` 或 `agent.max_turns`。AstraBox 把它们写入 Hermes 的 managed scope，它逐键
  覆盖配置目录自己的 `config.yaml`，Assistant 无法修改。AstraBox 自己设置的键会被拒绝：
  `model`、`providers`、`custom_providers`、`mcp_servers`、`terminal`、
  `timezone`（请设置 Assistant 的[时区](#时区)）、`plugins.enabled` 和 `plugins.disabled`。`skills` 也会被拒绝，因为 Hermes 不从
  managed scope 读取技能设置。

## 原生 Assistant 页面 {#panel-runtimes}

在控制台中选择一个 Assistant，即可打开其运行时的页面。这些页面需要 HTTPS 和 OpenSandbox
Secure Access。请把生命周期服务器的路由模式设为 `wildcard`，并使用以 `*.` 开头的网关地址。
OpenSandbox 1.2.1 的 ingress chart 使用 `gateway.gatewayRouteMode=header`，按请求的 Host
路由这些请求。控制台为归属用户签发短期有效的签名地址，形式为
`<sandbox>-<port>-<expiry>-<signature>.<gateway domain>`。请通过 DNS 验证为网关域名签发通配符证书。

### Hermes

默认情况下，在 Assistant 页面上选择**在新标签页打开**，即可使用官方 Hermes Webapp。它使用
Assistant 常驻的 Hermes Gateway，以及 AstraBox 更换沙箱时保存并恢复的同一个 profile 和
SessionDB。新对话、历史记录和继续聊天请使用 Hermes 自己的控件。每次打开时，AstraBox 都会
签发一个新的一次性 Hermes 登录链接。签名地址过期后，从 Assistant 重新打开 Webapp，获取新
地址并从历史记录继续。

请配置 HTTPS 反向代理，根据传入请求的 Host 设置 `X-Forwarded-Host`，并覆盖客户端提供的任何
值。HTTP 请求和 WebSocket 升级请求都要转发这个请求头，以便 Hermes 校验其来源。

设置 `ASTRABOX_HERMES_WEBAPP_EMBEDDED=true` 可在控制台内打开 Webapp。控制台和沙箱网关必须
位于同一个 HTTPS 站点。对于 9121 端口上经过签名的 Hermes Webapp 文档请求，请配置反向代理，
把上游 Content-Security-Policy 中的 `frame-ancestors 'none'` 替换为 `frame-ancestors` 加上
控制台准确的 HTTPS 源。保留其他指令，包括一次性登录页面的脚本 nonce。

运行时镜像需要包含内置的 Hermes Webapp。更新 RuntimeProfile 的镜像后，先暂停再恢复现有
工作区，让它们带着已保存的 Hermes 状态使用新镜像。

### dsh-superagent {#dsh-superagent}

[dsh-superagent](https://github.com/deepseek-ai/dsh-superagent-prototype)
运行在 DeepSeek Harness 上，从 Source 接收请求，把每个请求变成一个 Ticket，并交给归属用户
的某个 Agent 处理。它的 Web profile 在控制台的 Superagent 面板中打开。使用 harness 自己的
对话控件开始聊天、打开历史或继续已保存的 Session。Assistant 在两次访问之间保持运行，以便
回应它的 Source。

新的浏览器聊天，以及设为沿用 DSH 默认设置的 Superagent Session，都使用 Assistant 的模型
连接。在 **DSH default model settings** 中设置原生的 `reasoningEffort`，例如
`{"reasoningEffort": "xhigh"}`，即可选择模型连接提供的某个推理强度。未设置时，默认推理强度
来自模型连接（如果模型连接描述了它）。DSH 也可以在已有 Session 或 Superagent 的各角色设置中
保留显式选择。

部署需要：

- **网关域名与控制台位于同一站点。** harness 把登录状态保存在 `SameSite=Strict` cookie 中，
  浏览器只有在控制台与网关共享可注册域名和协议时，才会在控制台的 frame 中发送它：例如控制台
  位于 `https://astrabox.example.com`，网关位于 `*.sbx.example.com`，两者都使用 HTTPS。控制台
  会在 `ASTRABOX_SANDBOX_ENDPOINT_URL_TTL_SECONDS`（默认 15 分钟）到期之前更新签名地址，这会
  重新加载面板。
- **在 GitHub 上工作需要一个代理账号。** 把部署密钥 `astrabox-dsh-superagent-github-token`
  （对应的环境变量为 `ASTRABOX_DSH_SUPERAGENT_GITHUB_TOKEN`）设为一个机器人账号的令牌，所有
  dsh-superagent Assistant 都会使用它。希望以另一个账号完成工作的归属用户，可以在 Assistant
  的设置页设置 **GitHub agent account token**；它加密保存，只为该 Assistant 替换共享令牌。
  两者都没有时，代理没有 GitHub 账号：GitHub Source 保持未设置，其他 Source 照常运行。该账号
  必须在 GitHub 上接收提及通知：在它的通知设置中，**Participating, @mentions and custom**
  必须包含 **On GitHub**。令牌是带有 `repo` 和 `notifications` 权限范围的 classic personal
  access token。

要让 Assistant 开始工作，请把机器人账号邀请到仓库，然后在面板的 **Settings** 中设置 GitHub
Source 的仓库、它可以处理哪些账号的请求，以及准备 Ticket 检出目录的命令，例如：

```bash
git clone --quiet "https://github.com/$DSH_SUPERAGENT_GITHUB_REPOSITORY.git" "$DSH_SUPERAGENT_DIR" \
  && git -C "$DSH_SUPERAGENT_DIR" checkout -q -b "agent/$DSH_SUPERAGENT_NAME"
```

允许的账号在 issue 中提及机器人后，这条提及会成为一个 Ticket；机器人在该 issue 上回复，
代码改动以 pull request 的形式提交，由人来合并。bundle 自己的模型调用通过部署的模型网关
使用 Assistant 的模型。

具体工作由归属用户的某个 Agent 完成。对于每个 Ticket，bundle 的主 Session 选出名称和描述
与请求相符的 Agent，该 Ticket 会以归属用户的身份通过 [A2A](a2a.md) 成为这个 Agent 的一段
对话。归属用户可以像其他对话一样在控制台中看到它，Ticket 页面上的 **Open conversation**
会打开它；请求的后续内容（例如 issue 上的新评论）进入同一段对话。Agent 在自己的沙箱中用
自己的程序和模型工作，上面的准备命令在它的工作区中运行，因此面板的执行设置不包含 bundle
自己的 worker 模型、执行目录和禁止的命令。Agent 用它的 `report_outcome` 工具报告每条消息的
结果（[任务结果](a2a.md#task-outcomes)）：

- **完成。** 它的报告、它推送的分支和最后一次提交，以及它的回复，构成 Ticket 的结果；
  Assistant 用这个结果在线程中回复请求，并就某个人的后续消息回复那个人。它提到的、来自
  Ticket 分支的 pull request 会成为该 Ticket 跟踪的对象。没有报告的回复视为完成。
- **拒绝。** Ticket 以 Agent 给出的原因结束为 dropped，Assistant 告诉对方原因。
- **交回。** Agent 表示请求应由另一个 Agent 处理或必须拆分。Ticket 线程上的后续消息会带着
  Agent 的原因回到 bundle 的主 Session，由它重新分派，Ticket 保留已有的工作。Ticket 自己的
  请求不能重新分派：bundle 只从一条消息启动一个 Ticket，因此该 Ticket 会像被拒绝的一样以
  dropped 结束，并说明它已被交回，由对方重新提出请求。

Agent 提出的问题或等待的审批显示在 Ticket 上，并在 Agent 的对话中回答。停止 Ticket 会取消
其对话中的工作（[取消](a2a.md#interaction-boundaries)）。Assistant 可以选择的 Agent，是其运行时启动时归属用户可以
使用的 Agent；之后新增的 Agent 在下一次启动后才会提供。

Agent 在每个有意义的步骤之后都提交并推送 Ticket 的分支，而不只是在结束时，因此它的工作在
Agent 的工作区之外也能保留：在没有这些工作的工作区中开始处理某个 Ticket 的 Agent（例如在
没有工作区卷的新沙箱中），会在远端查找该 Ticket 的分支并从那里继续。bundle 的主 Session
把来自其他 Ticket 的事实告诉某个 Ticket 的 Agent 并点名这些 Ticket 时，消息会引用这些
Ticket 的最新任务（`referenceTaskIds`），Agent 会拿到其中每个 Ticket 最后报告的内容。

让做代码工作的 Agent 以机器人的身份访问 GitHub。bundle 只跟踪机器人从 Ticket 分支打开的
pull request，因此以其他账号工作的 Agent 打开的 pull request 不会被它的 Ticket 跟踪。管理员把
机器人的令牌在一个 Vault 中添加两次，并把这个 Vault 分配给这些 Agent
（[使用 Vault 认证](credentials.md#2-add-a-credential)）：

- 作为仓库 HTTPS URL（例如 `https://github.com/your-org/your-repo.git`）的
  **HTTP Basic（Git HTTPS）** 凭证，用户名为机器人的登录名，供 `git push` 使用；以及
- 作为 `api.github.com` 的**环境变量**凭证，名为 `GH_TOKEN`，供 `gh pr create` 使用。

这些 Agent 的沙箱镜像需要 `git` 和 `gh`；Claude Code 镜像两者都有。

bundle 把它的 Ticket、线程和决定保存在自己的数据库中。平台在沙箱工作期间把这个数据库连同
harness 的 Session 日志和设置保存到平台数据库，并在沙箱释放或丢失后把它们恢复到新沙箱中；
Ticket 的检出目录会重新准备。

限制：

- 机器人读取每个 Assistant 指定仓库的通知，并且从不把它们标为已读，因此只要每个 Assistant
  关注各自的仓库，多个 Assistant 就可以共用一个机器人。没有机制阻止两个 Assistant 指定同一个
  仓库，Assistant 也可以指向机器人受邀加入的任何仓库。
- [Assistant 默认配置](#defaults)中的 Skill、MCP 服务、软件包和 Agent 程序配置是为默认配置
  所选 RuntimeProfile 的程序编写的。使用其他程序的 Assistant 不会继承它们。默认配置的模型
  仍然适用；dsh-superagent 没有放置组织指令的位置。
- 飞书是 bundle 的第二个 Source，平台不会配置它。
- 进程保留它启动时的环境：添加到已分配 Vault 中的环境变量凭证会在 Assistant 的运行时下次
  启动时到达 bundle，而轮换或归档的凭证会对正在运行的 bundle 生效
  （[保护凭证](egress-credential-injection.md#添加出站凭证)）。

### 你自己的 Source {#own-sources}

Source 是一个 dsh-superagent 插件，读取某个服务的事件并在该服务上回复
（[bundle 的 cookbook](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/008-cookbook.md#add-a-source)）。
AstraBox 的镜像为 bundle 打了补丁，使 GitHub 成为可选项，并让 Source 可以位于自己的软件包中，
从 `dsh-superagent/sources` 导入 bundle 的 Source 接口
（`containers/sandbox-dsh-superagent/upstream-changes.patch`）。组织在 AstraBox 的镜像之上构建
自己的镜像，即可加入自己的工单或待办服务：

```dockerfile
FROM ghcr.io/colton-z/astrabox-sandbox-dsh-superagent:<version>
# 插件软件包及其自身依赖；`dsh-superagent` 解析为已安装的 bundle。
COPY --chmod=u=rwX,go=rX my-tickets-source /opt/astrabox/dsh-plugins/my-tickets-source
# 它的加载层：按名称顺序插入在 AstraBox 的加载层之后。
COPY --chmod=0444 50-my-tickets.yml /opt/astrabox/superagent/patches.d/50-my-tickets.yml
# 启动一次组合，并要求该 Source 类型已经安装。
RUN /usr/local/bin/astrabox-dsh-superagent-boot-check --source my-tickets
```

加载层例如：

```yaml
- insert:
    - id: my-tickets-source
      name: file:///opt/astrabox/dsh-plugins/my-tickets-source/lib/index.js
```

该检查以非特权账号启动 harness，与沙箱在 Assistant 自己的账号下运行它的方式一致，因此该账号
无法读取的插件文件会让构建失败，而不会让 Source 悄无声息地处于未安装状态；请让插件文件对所有人
可读，就像 `--chmod=u=rwX,go=rX` 所做的那样。把一个使用 `dsh_superagent` 引擎的 RuntimeProfile
指向这个镜像。Source 的设置显示在面板的 **Settings** 中。插件通过分配给 Assistant 的 Credential
Vault 中的凭证访问它的服务：绑定到该服务主机的 `environment_variable` 凭证会在插件的环境中
提供一个占位符，沙箱的出站代理会在匹配的请求中把它替换掉
（[保护凭证](egress-credential-injection.md#添加出站凭证)）。RuntimeProfile 的网络访问必须
允许该服务的主机。

归属用户也可以给自己的 Assistant 添加 Source，无需自己的镜像：把它打包成一个 dsh 插件
Bundle，由 Bundle 自己的 `cordis.patch.yml` 插入插件，并在 Assistant 的**技能、插件与配置**
卡片中的 **Profile package.json** 下列出它，例如
`{"dependencies": {"my-tickets-source": "1.2.0"}}`（npm 版本、git 规格或 tarball URL）。保存后，
Assistant 会在它的沙箱上重新准备：harness 自己的 `dsh plugin` 安装 Bundle，然后 harness 重启；
安装失败时，启动会停止并显示 pnpm 自己的消息。每个新沙箱都会按配置重新安装 Bundle，它们不属于
保存的状态。[Assistant 默认配置](#defaults)可以为使用该程序的每个 Assistant 列出 Bundle。网络
访问受限时，公共 registry 或 git 主机会为该 Assistant 放行；私有的 registry 或主机（例如内部
registry）只有在管理员把它写入 RuntimeProfile 的允许主机后才会放行。

## 修改何时生效

保存修改不会中断正在生成的回复。修改何时到达某个对话，取决于 Hermes 在什么时候读取
这项设置：

- **系统提示词。** Hermes 在对话开始时构建该对话的提示词。AstraBox 把新的提示词写入
  Hermes 的配置目录：修改之后开始的对话使用它，已经打开的对话保留开始时的提示词。
- **模型。** Hermes 在每次回复开始时检查所配置的模型。AstraBox 把新的模型写入
  Hermes 的配置目录：新对话使用它，已经打开的对话从下一条消息起切换到它。正在生成的
  回复仍用开始时的模型完成。
- **技能。** Hermes 在对话开始时列出该对话可用的技能：修改之后开始的对话使用新的
  技能集合。
- **时区、MCP 服务器、扩展包、Hermes 配置，以及沙箱自己持有的模型凭证。** Hermes 只在
  程序启动时读取，因此 AstraBox 会重启它。重启会中断该 Assistant 其他对话中所有正在进行的回复，所以
  AstraBox 会先等到没有任何工作在运行，包括等待审批或回答的回复以及后台委派任务。
  在此之前，新对话显示为准备中。如果 30 分钟后仍有工作在运行，新对话会失败，报错中会
  列出这些工作，并且不会重启。新增 MCP 服务器的主机也会加入工作区可访问的范围。

重启之后，该 Assistant 的其他对话在下一条消息时继续，历史保持不变。如果 Hermes 因
其他原因在回复过程中重启，该回复会以错误结束，并说明后端已不再持有它；对话仍可接收
下一条消息。

无法完成准备的对话会结束并显示原因，例如 Assistant 指定了 MCP 服务器，但其
RuntimeProfile 不允许使用 MCP 服务器。排除原因后，请开始新的对话。

## 开始并继续工作

在左侧栏选择 Assistant，会打开它的**主对话**：每次都是同一段，Assistant 还没有时就新建；
AstraBox 会按需准备或恢复工作区。它的例行任务和盯梢都会把结果发到这里；归属用户不在
这段对话时收到回复，左侧栏里这个 Assistant 会标为未读。主对话就是这一行，所以不会在
Session 列表中再列一次。

发出第一条消息之前，对话会列出 Assistant 能用的东西：为它连接的应用、它自己的和继承的
Skill 与工具，以及归属用户还可以连接的应用。对话标题栏中的 **Assistant** 打开它的资料页：
名称、时区、归属用户的指令、为它开关的已连接应用、继承的内容，
以及删除。参见[例行任务与盯梢](#routines-and-watches)。

结束主对话就是重新开始：下次打开这个 Assistant 时会开始一段新的主对话。通过 API 仍可以用
`POST /api/v1/assistants/{assistant_id}/conversations` 开始更多对话，每段都是挂在同一工作区上的
另一个 Session。

Agent 程序定义自身的原生状态，AstraBox 将其保存到平台数据库以供恢复。每段对话也有
独立的 Session 历史。结束一段对话不会结束 Assistant 或清空共享工作区；沙箱更换后
是否保留工作区文件，取决于是否配置持久工作区存储。

消息、审批、问题、文件和分享链接都使用 [Session](sessions.md) 中介绍的统一界面。

## 从消息应用使用 {#triggers}

Assistant 可以有自己的触发配置，和 Agent 的 Deployment 一样：Telegram、Slack 等消息渠道或 Webhook。它的定时任务则是归属用户的个人计划，参见[例行任务与盯梢](#routines-and-watches)。打开**管理台 → 触发配置**，选择**新建触发配置**，在 **Agent 或 Assistant** 中选择这个 Assistant。只有 Assistant 的归属用户可以创建或修改它的触发配置，由此开始的每次对话都以归属用户身份运行。

消息平台的机器人谁都能私聊，所以 Assistant 的消息渠道触发配置必须在**允许的发送者**中填写归属用户自己的账号，每行一个，原样使用所选适配器的发送者格式和示例。例如，Satori 的 Telegram 适配器使用 `telegram:123456789`；其他适配器可能使用不带前缀的账号，参见[允许的发送者](channels.md#允许的发送者)。其他账号的消息会被忽略，不会进入 Assistant，也不会成为之后回复的上下文。

每个消息渠道会话对应共享工作区中的一次 Assistant 对话。回复会发回这个聊天；之后 Assistant 在这次对话里主动产生的内容也会发回去，例如盯梢任务的结果、完成的后台任务，或 Hermes 自己发起的回复。

## 例行任务与盯梢 {#routines-and-watches}

Assistant 不用每次吩咐就会做两类事，归属用户在不同的地方管理它们。

**例行任务**是归属用户点了时间的事，例如"每个工作日 8 点汇总我的未读邮件"。Assistant 用 `create_schedule` 工具把它建成一个个人计划，由平台调度器以归属用户身份运行。每次运行都是 Assistant 主对话里一条标明由定时任务发出的消息，回复也在主对话里；归属用户正在对话时，这次运行会排队等候。例行任务在账号菜单的**个人计划**里查看、编辑、停用、立即运行和删除，和 Assistant 交给 Agent 的计划列在一起（[个人计划](a2a.md#personal-agent-schedules)）。例行任务不依赖工作区正在运行：平台调度器触发它，主对话像处理任何消息一样准备工作区。

**盯梢**是 Assistant 替你关注某件事，只在值得说的时候才开口，例如"经理的邮件里提到截止日期就告诉我"。Hermes 在 Assistant 工作区里用它自己的调度器运行这些任务，AstraBox 会把每次运行的结果报告回创建任务的那次对话：消息渠道聊天会立即收到，管理台中的对话会在列表里显示为未读，开启通知时还会弹出浏览器通知。回复 `[SILENT]` 的运行（盯梢任务在没有值得报告的内容时这样回答）会保留但不会发送。任务和 Hermes 的其他 profile 状态一起保存在平台数据库中，更换工作区的沙箱后仍然保留。

可以让 Assistant 通过 Hermes 原生任务工具修改或删除某个盯梢任务。

每个 Assistant 还带有 AstraBox 的 `astrabox-proactive` 技能，随 Hermes 镜像提供。它教 Assistant 在发现重复性工作时（晨间简报、重要邮件盯梢、会前准备、每周回顾）主动提出，按归属用户有没有点时间来区分例行任务和盯梢，只在用户同意后创建，把每一个都写成能借助用户已连接的应用独立运行，并让盯梢任务在有值得报告的内容之前保持安静。

## 连接办公应用 {#connectors}

Assistant 管理页面上的 **Connectors** 卡片用来连接办公应用：Gmail、Google 日历、Google Drive、Notion、Linear、Jira 和 Confluence、GitHub 以及 Slack。

点击 **连接**，在厂商那里登录授权。浏览器会回到列出每个人连接的 **连接** 页面，这个
应用随即在此人的每个 Assistant 中启用。新建的 Assistant 会带上属主已经连接的应用。在某个
Assistant 上关闭一个应用后，属主之后再次连接它，这里仍保持关闭；断开连接不会关闭任何
应用，这样 agent 可以告诉对方需要重新连接。

连接由本部署的 connector hub [ContextForge](https://github.com/IBM/mcp-context-forge) 保存，它和 AstraBox 一起运行。hub 保存每个人在厂商那里的 token、负责刷新，并带着这个 token 调用厂商。agent 只能通过 AstraBox server 使用 connector：server 以这段对话所代表的人的身份把每次调用转给 hub，这个人是 Assistant 的属主，或者发起 Agent 对话的人。沙箱里既没有任何 token，也没有 hub 的地址，所以 connector 不需要 RuntimeProfile 开放 MCP 网络访问。连接属于创建它的账号；每个 Assistant 和 Agent 只使用为它启用的 connector。agent 需要一个这个人还没连接的应用时，可以在对话里展示一张卡片，请对方连接。启用或停用 connector 后，Assistant 的 Hermes 后端会在没有回复进行时重启，和其他 MCP 修改一样。

**断开** 会从 hub 删除 token，但不会撤销厂商那边的授权；要撤销，还需要在厂商的账号设置里移除这个应用。厂商不再接受这个连接时，卡片会显示 **需要重新登录**。

Notion、Linear、Jira 和 Confluence、GitHub 和 Slack 由厂商自己的 MCP 服务器提供。Google 的 Gmail、日历和 Drive MCP 服务器只以开发者预览形式提供，其条款禁止提供给你自己组织以外的用户，所以这三个应用由本部署自己的 [google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp) 实例提供，它用每个人的 token 调用 Google 正式发布的 API。

### 配置 connector

管理员在 **管理控制台 → Connectors** 中为每个应用配置一次：

1. 厂商只给注册过的应用发 OAuth 客户端时（Google、GitHub 和 Slack），用页面上显示的回调地址（hub 的地址加 `/oauth/callback`）在厂商那里注册一个 OAuth 应用，并填入它的 client ID 和 client secret。Notion、Linear 和 Atlassian 会自动为 hub 注册。
2. 点击 **配置**。
3. 点击 **连接你的账号**，在厂商那里登录。hub 只有在有已登录账号时才能读取应用的工具。
4. 点击 **发布工具**。之后所有账号都可以使用这个应用。

再次保存应用的 OAuth 设置会移除已发布的工具，之后需要重新发布。

AstraBox 向 Google 申请的权限与 ChatGPT 中同一产品的应用相同：Gmail 可以读取、起草和发送邮件但不能永久删除，日历可以读取日历和修改事件，Drive 可以管理文件。它还会申请账号的邮箱地址，google_workspace_mcp 用它向 Google 校验每个 token。

Google 把 Gmail 和 Drive 的访问视为受限权限：应用如果要给你自己 Google Workspace 组织以外的账号使用，需要通过 Google 的验证（包括每年一次的第三方安全评估），否则只有测试用户可以连接。

## 长对话

对话变长后，Hermes 会在回复进行中按自己的阈值压缩较早的上下文。AstraBox 不另行请求空闲压缩，避免摘要占住原生网关而阻塞下一条消息。模型的上下文窗口由 Hermes 自己判断：优先使用模型端点报告的值，端点不报告时按模型名称确定。

## 暂停与恢复工作区

Assistant 不需要占用计算资源时，可以在详情页选择**暂停工作区**。AstraBox 会停止
原生状态写入，确认 Agent 程序状态已保存到平台数据库，然后释放沙箱。此操作不依赖
持久工作区卷。需要跨暂停保留工作区文件时，应配置持久卷，或先导出文件。

选择**恢复工作区**或直接开始新的对话，即可把保存的原生状态恢复到一个新沙箱中。
保存的状态包括 Hermes 的会话库和 profile：记忆、它创建或安装的技能，以及定时任务。
镜像自带的技能会重新预置，Hermes 配置会按 Assistant 设置重新写入。配置持久工作区存储后，
同一个 Assistant 的文件会挂载到新沙箱；未配置时，其他文件从空白开始。Assistant 的这项操作会释放并重新分配计算资源，不调用 OpenSandbox
的沙箱暂停与恢复操作。

工作区的沙箱在未暂停的情况下消失时（例如所在节点故障），即使没有人打开 Assistant，也会在几分钟内被替换：AstraBox 会把保存的原生状态恢复到新沙箱中，对话和消息渠道随即可以继续回复。沙箱丢失时正在进行的回复会以错误结束，不会重试。替换后的沙箱包含恢复操作会恢复的内容：Hermes 保存的状态，以及（仅在配置持久化工作区存储时）工作区文件。丢失前最后几秒内 Hermes 做出的修改可能缺失。

管理台会显示工作区处于未启动、启动中、就绪、已暂停或需要处理状态。只有保存完成并确认
沙箱已经释放后，暂停操作才会报告成功。任一步骤无法确认时，Assistant 会保留，以便重试。

## 鉴权与凭证

只有归属用户可以查看、打开、修改、暂停、恢复或删除 Assistant。其他账户访问时，会收到
与 Assistant 不存在时相同的未找到响应。

凭证传递与归属鉴权相互独立。使用已分配的 MCP 凭证需要开启沙箱凭证保护。沙箱后端
只会在匹配的出站请求中加入凭证，保存的值留在 Assistant 沙箱之外。关闭凭证保护时，
需要认证的受管理 MCP 配置会被拒绝。

管理员可以在**管理台 → 凭证**中为 Assistant 分配 Credential Vault（凭证库）。Assistant
可以使用其中分配的 MCP 凭证，但用户和 Agent 程序都看不到保存的凭证值。支持的凭证类型
和分配规则见[凭证](credentials.md)。

## 删除 Assistant

删除前，请先导出需要另外保存的文件。可以在资料页或管理页删除。只有确认当前沙箱已经
销毁后，AstraBox 才会移除 Assistant 记录。如果无法确认销毁，记录会继续保留，用户可以
重试删除。删除主 Assistant 后，归属用户下次打开控制台时会按 Assistant 默认配置得到一个新的。

## 通过 API 使用

创建 Assistant、开始对话、管理工作区生命周期和删除 Assistant 也可以通过 HTTP API
完成。系统提示词对应 Assistant 的 `system` 字段，与 Agent 相同。每个 AstraBox 实例都在 `/docs` 提供当前版本的交互式 API 参考，并在
`/openapi.json` 提供 OpenAPI 文档。鉴权和请求约定见 [API 概览](api.md)。

在终端或脚本中，`astrabox assistant` 通过同样的接口读取 Assistant、修改其模型和 Agent
程序配置、设置其运行时要求的凭据，以及暂停或恢复其工作区（见 [CLI 命令详解](cli/commands.md#astrabox-assistant)）。

## 相关指南

- [RuntimeProfile](runtime-profiles.md)
- [Session](sessions.md)
- [消息渠道接入](channels.md)
- [Schedule](schedules.md)
- [凭证](credentials.md)
- [连接模型服务](models.md)
