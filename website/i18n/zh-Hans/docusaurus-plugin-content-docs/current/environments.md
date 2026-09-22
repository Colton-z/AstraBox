# 运行环境

Environment 定义 Session 使用的运行环境，包括 Agent 程序、沙箱镜像、
模型连接和网络访问。你可以选择当前 AstraBox 部署中已有的 Environment，也可以
为特定任务创建新的 Environment。

## Environment 是什么

Environment 是 Session 的基础设施层：

- **Agent 程序** - 驱动 Agent 的已安装程序，以及运行它的沙箱服务
- **沙箱镜像** - 预先构建的镜像，其中包含 Agent 程序、系统依赖、语言
  运行时、系统账号和后台服务
- **连接与控制** - 模型服务、出站网络访问、沙箱生命周期和可选的链路追踪
  设置

Session 启动时，AstraBox 会根据指定的 Environment 模板创建或分配沙箱。

## 字段说明

控制台提供以下设置。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| 名称 | 是 | Environment 的唯一名称，创建后不能改名。 |
| 显示名称 | 否 | 创建 Agent 时向用户显示的 Environment 名称。 |
| 描述 | 否 | 这个 Environment 适合什么场景。 |
| Agent 程序 | 是 | 驱动这个 Environment 中 Agent 的已安装程序。 |
| 启用 | 否 | 创建 Agent 时能否选择这个 Environment。 |
| 沙箱服务 | 否 | 创建和管理沙箱的服务；AstraBox 支持 OpenSandbox。 |
| 沙箱镜像或模板 | 否 | 预先构建的沙箱镜像；留空时使用所选 Agent 程序的部署默认值。 |
| 网络访问 | 否 | “受限”允许平台必需的连接和指定目标，“不受限”允许访问所有出站目标。 |
| 沙箱闲置处理 | 否 | 终止闲置沙箱；沙箱服务支持快照时，也可以暂停后再恢复。 |
| 沙箱使用方式 | 否 | 每段对话使用独立沙箱，或由同一 Agent 的多段对话共享一个沙箱。 |
| 沙箱权限 | 否 | 授予 Agent 程序和沙箱使用方式所需的系统权限。 |
| 模型连接 | 否 | 为这个 Environment 中的 Agent 选择模型网关及其连接信息。 |
| 链路追踪 | 否 | 将 Agent 程序支持的 OpenTelemetry 数据发送到你的采集服务。 |

## 运行配置

AstraBox 是自托管软件，因此 Environment 不需要在厂商托管云和自托管 worker
之间切换。它选择当前 AstraBox 部署提供的基础设施：Agent 程序、沙箱服务
和镜像，以及模型连接。

大多数安装可以在创建 Agent 时直接选择已有 Environment。Agent 需要不同的
Agent 程序、镜像、模型连接、网络规则或沙箱生命周期时，部署管理员可以创建新的
Environment。

预热配置在 Agent 上，而不是 Environment 上。启用后，AstraBox 会根据所选
Environment 提前准备完整的 Agent 运行时。两种沙箱使用方式都使用 OpenSandbox 官方
SDK client pool。Agent tenancy 在共享沙箱内准备隔离的引擎运行位置；conversation
tenancy 在池发布可用沙箱前完成整箱准备，之后由 Session 领取。

## 预装依赖

请在 Session 启动前把软件安装到沙箱镜像中。例如，可以基于已安装的 Agent
镜像添加系统依赖、Python 包和 Node.js 包。基础镜像请使用与你运行的 AstraBox
版本相同的标签，因为镜像内的平台组件必须与服务端匹配：

```dockerfile
FROM ghcr.io/colton-z/astrabox-sandbox-claude-code:0.1.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential libssl-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir pandas numpy scikit-learn
RUN npm install -g typescript eslint prettier
```

| 包管理器 | 镜像构建命令 | 常见用途 |
| --- | --- | --- |
| apt | `apt-get install` | Debian/Ubuntu 系统依赖 |
| pip | `pip3 install` | Python 包 |
| npm | `npm install -g` | Node.js 包和 CLI |

> 生产环境应固定依赖版本和基础镜像的标签或 digest。把依赖构建到镜像中，既能
> 保证按需创建和预热的沙箱一致，也不需要在 Session 启动时重新安装。

## 启动脚本

AstraBox 的 Environment 不提供通用 setup script。每个沙箱都需要的软件、
系统账号和服务应放进镜像。这样，即使沙箱早于 Session 创建并已准备好，这些能力也
已经可用。

只属于某个 Agent 的代码应放在它的 Git 仓库中，可复用的 Agent 行为应通过
Plugin 和 Skill 提供。沙箱文件系统和镜像约定见
[容器参考](container-reference.md)。

## 创建 Environment

1. 在 AstraBox 控制台中打开 **运行环境**。
2. 选择 **创建环境**。
3. 填写名称并选择 Agent 程序，然后按需配置沙箱镜像、模型连接、网络访问
   和生命周期选项。
4. 选择 **创建**。

只要 Environment 已启用，并且所选 Agent 程序支持 Agent，创建或编辑
Agent 时就可以选择它。

## 查询 Environment

在 AstraBox 控制台中打开 **运行环境**，可以查看包括停用项在内的所有
Environment。列表会显示名称、Agent 程序、沙箱服务和最后更新时间。选择
一项即可查看完整配置。

Agent 创建者只能看到已经启用、并且能够运行 Agent 的 Environment。模型和
链路追踪凭证不会出现在这个选择列表中。

停用 Environment 后，不能再创建新的 Agent 对话或重新预热。AstraBox 会回收
其 Agent 尚未被领取的预备容量，不会因此回收已经分配给 Session 的沙箱。
管理员仍可查看预热状态和删除 Agent；即使 Agent 保存的预热开关仍然开启，
状态也会显示预热已停用、可用数量为零。

## 更新 Environment

打开 Environment，修改所需区域，然后选择 **保存**。

> 更新 Environment 不会原地修改正在运行的沙箱。AstraBox 下次为使用该
> Environment 的 Agent 创建、分配或重建沙箱时，会使用保存后的配置；依赖该
> Environment 的 Agent 预备容量也会重新协调。

## Environment 选型建议

| 场景 | 推荐配置 |
| --- | --- |
| 通用开发 | 使用适用于所需 Agent 程序的现有 Environment。 |
| 数据分析 | 使用包含固定版本 Python 和系统依赖的镜像，并且只允许访问必要的数据服务。 |
| 前端开发 | 使用包含所需 Node.js 工具链的镜像；网络受限时允许访问对应的包仓库。 |
| CI/CD 集成 | 使用包含所需 CLI 的镜像，连接所需凭证，并且只允许访问目标服务。 |

## 常见问题

**Q：Environment 创建后需要等待多久才能使用？**

A：新的 Environment 可以立即使用。Session 需要运行时，AstraBox 才会创建或
分配实际沙箱；预热可以缩短这段启动时间。

**Q：预装包的版本可以指定吗？**

A：可以。在沙箱镜像的构建配置中固定版本，并使用不可变的镜像标签或 digest，
让新建和预热的沙箱使用同一套软件。

**Q：最多能创建多少个 Environment？**

A：AstraBox 本身不设置数量上限。请按部署实际需要创建，并使用清晰的命名规范
进行管理。

## 下一步

- [启动 Session](sessions.md) - 使用 Agent 开始一项任务
- [定义 Agent](authoring-agents.md) - 查看 Agent 配置
