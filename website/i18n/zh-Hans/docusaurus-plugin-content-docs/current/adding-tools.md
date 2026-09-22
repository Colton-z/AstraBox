# Agent MCP、Skill 和 Plugin 配置

MCP 服务器、Skill 和 Plugin 可以扩展 Agent 的能力。在创建或更新 Agent 时配置它们，让 Agent 可以访问外部服务、使用领域流程和打包扩展。

## 扩展的作用

读写文件、编辑代码、执行命令和搜索等原生工具由所选 Agent 程序提供。AstraBox 不使用单独的 `tools` 字段复制一套工具定义。

Agent 配置可以增加三类扩展：

- **MCP 服务器**提供实时函数和数据。
- **Skill**提供可复用的提示词、流程和配套文件。
- **Plugin**把 Skill、命令和 MCP 定义打包在一起。

AstraBox 首次准备或重建 Session 运行实例时，会在沙箱中准备这些扩展。Agent 程序根据任务决定何时使用。

预热可以在 Session 启动前准备好扩展。要按已保存的配置重新拉取 Plugin 和 Skill，
请在 Agent 上使用「重新预热」。它只替换待领取资源，现有 Session 保留其运行实例；
固定的 Commit 版本不会自动升级。

## 可用扩展

MCP 服务有两种连接类型：运行在沙箱外的远程 HTTP 服务，或运行在沙箱内的本地 stdio
命令。连接类型与配置来源是两个独立维度。管理员集中配置的远程 MCP 服务仍然是远程
MCP 服务；集中管理只会改变由谁维护它的定义。

| 扩展 | 用途 | 典型场景 |
| --- | --- | --- |
| 远程 MCP 服务器 | 调用运行在 Session 沙箱之外的 MCP 服务 | 数据库、工单系统、文档服务、内部 API |
| 本地 stdio MCP 服务器 | 在 Session 沙箱中启动 MCP 命令 | 文件系统工具、本地分析器、命令行集成 |
| Skill | 提供专门流程和配套文件 | 代码审查、发布检查、报告生成 |
| Plugin | 安装经过审核的 Skill、命令和 MCP 定义组合 | 团队扩展包和领域工作流 |

注意事项：

- 只要服务器运行在沙箱之外，它就是**远程 MCP 服务器**，无论配置保存在 Agent、由管理员统一管理，还是来自 Plugin。这些方式只改变谁维护配置。
- 所选 Agent 程序决定支持哪些扩展格式。如果它不能使用某项配置，AstraBox 会拒绝保存这个 Agent。
- 保存 Agent 不会重新配置正在执行的任务。AstraBox 在首次准备或重建 Session 运行实例时使用最新的扩展配置。
- 网络规则和凭证与 MCP 定义是两项独立配置。沙箱必须能够访问远程服务器，受保护凭证也必须匹配准确的目标地址。

## 原生能力与浏览器能力

浏览器和 Web 能力属于所选 Agent 程序，或属于它支持的扩展。AstraBox 没有一套带有独立工具名的平台级浏览器工具集。

Agent 程序本身支持浏览器时，使用它的原生配置；否则添加浏览器 MCP 服务器或经过审核的 Plugin，并在 Environment 网络策略中允许所需的远程地址和包下载地址。

接入浏览器能力时：

- 使用 Agent 程序或 MCP 服务器定义的名称和权限规则；
- 修改 Agent 不会改变正在运行的任务；AstraBox 下次准备或重建 Session 运行实例时使用最新设置；
- 验证一次真实浏览器操作，以及扩展提供的实时预览入口；
- 不要假设其他 Agent 程序具有相同的浏览器能力。

## 当前格式

Agent 级扩展使用所选 Agent 程序支持的原生格式。打开「管理台 > Agent」，创建或打开一个 Agent，然后使用「MCP、Skill 与 Plugin」部分。项目代码仓库单独配置在「工作区配置」中。

MCP 服务定义保存服务名称、URL、transport 和允许发送的请求头。密钥值保存在 AstraBox
Credential Vault 中，并把该 Vault 分配给 Agent。运行时由沙箱直接连接 MCP 服务；所选
provider 只在符合规则的受保护出站请求上加入凭证，让密钥留在沙箱之外。

![在 AstraBox 控制台配置 MCP 服务器、Skill 和 Plugin 仓库](./img/agent-create-console-zh.png)

对于已有 Agent，还可以在「MCP 服务与 Skill」卡片中选择管理员配置的远程 MCP 服务器和 Skill。直接配置字段使用下面的格式：

```json
{
  "mcp_servers": {
    "public-data": {
      "type": "http",
      "url": "https://mcp.example.com/mcp"
    }
  },
  "skills": [
    "https://github.com/example/agent-skills.git@REVIEWED_COMMIT_SHA#skills/code-review"
  ],
  "plugin_repos": [
    {
      "url": "https://github.com/example/agent-plugins.git",
      "sha": "REVIEWED_COMMIT_SHA",
      "plugin_paths": ["plugins/code-review"]
    }
  ]
}
```

系统提示词和所有扩展都可以不配置。只选择这个 Agent 需要的能力，然后点击「创建」或「保存」。

## 扩展配置示例

### 最小配置（仅远程 MCP）

```json
{
  "mcp_servers": {
    "public-data": {
      "type": "http",
      "url": "https://mcp.example.com/mcp"
    }
  }
}
```

### 完整开发环境

```json
{
  "mcp_servers": {
    "workspace-files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
    },
    "public-data": {
      "type": "http",
      "url": "https://mcp.example.com/mcp"
    }
  },
  "skills": [
    "https://github.com/example/agent-skills.git@REVIEWED_COMMIT_SHA#skills/code-review"
  ],
  "plugin_repos": [
    {
      "url": "https://github.com/example/agent-plugins.git",
      "sha": "REVIEWED_COMMIT_SHA",
      "plugin_paths": ["plugins/development"]
    }
  ]
}
```

## 更新 Agent

在「管理台 > Agent」中打开 Agent。在扩展卡片中修改所选 MCP 服务器或 Skill，或修改直接配置的 MCP、Skill 和 Plugin 仓库字段，然后点击该部分的「保存」。

:::note
保存不会重新配置正在执行的任务。AstraBox 在首次准备或重建 Session 运行实例时使用最新保存的 Agent。如果其他用户先保存了同一个 Agent，控制台会提示冲突，不会覆盖对方的修改。
:::

## 查看当前扩展配置

在「管理台 > Agent」中打开 Agent。管理员配置的 MCP 服务器和 Skill 显示在「MCP 服务与 Skill」卡片中；直接配置的 MCP 定义、Skill 来源和 Plugin 仓库显示在「MCP、Skill 与 Plugin」部分。

## 常见问题

**Q：不配置扩展会怎样？**

A：Agent 程序仍然保留自己的原生工具，只是 Agent 中没有配置额外的 MCP 服务器、Skill 或 Plugin。

**Q：能否在 Session 级别覆盖扩展配置？**

A：目前不支持。扩展配置属于 Agent。AstraBox 在首次准备或重建 Session 运行实例时读取当前 Agent 配置。需要另一组扩展时，请使用另一个 Agent。

**Q：扩展的顺序重要吗？**

A：Agent 程序根据任务决定何时使用扩展。程序通过名称访问扩展时，名称必须唯一。

**Q：扩展格式会随时间变化吗？**

A：格式属于 Agent 程序、MCP 协议、Skill 规范或 Plugin 格式。请固定经过审核的 Git 版本，并关注相应上游项目的版本说明。
