# 访问 GitHub

> 把 GitHub 仓库克隆到 Session 工作区，让 Agent 直接读取、修改代码并创建
> Pull Request。

AstraBox 支持为 Agent 配置一个默认 GitHub 仓库。平台在准备新工作区时，会在
第一条消息前克隆仓库；启用预热时，这可以发生在 Session 领取工作区之前。
Agent 可以像在本地工作树中一样读取、修改、提交和推送
代码。对于只使用一次的公开仓库，可以直接在任务中提供 HTTPS URL，让 Agent
自行克隆。

仓库工作树的归属与 Session 一致。修改 Agent 的仓库 URL、分支或克隆深度，
不会替换正在运行的沙箱中的工作树。更新后的仓库配置用于新工作区；恢复已有
工作区不会重新克隆或替换工作树。需要独立工作树时，请创建新的 Session。

## 核心流程

1. **准备仓库凭证。**私有默认仓库使用 SSH Deploy Key，并只授予任务需要的
   读取、写入等权限。把私钥保存在 AstraBox 服务环境中。
2. **在 Agent 上配置仓库。**在**项目代码仓库**中填写 SSH URL 和 Deploy Key
   的 Secret 名称。只处理一次公开仓库时，可以直接在用户消息中提供 HTTPS
   URL。
3. **Agent 在工作树中处理代码。**Session 启动后，Agent 可以使用所选 Agent
   程序原生的能力读取和修改代码。
4. **（可选）创建 Pull Request。**Agent 在仓库目录中使用 `git push` 推送
   分支，再通过已经配置的 GitHub MCP 服务、GitHub API 或沙箱镜像中的 `gh`
   CLI 创建 Pull Request。

:::note
仓库使用 Session 工作区。请及时 Commit 和 Push，或下载重要 Patch 和产物。配置持久化工作区后，工作树可以跨沙箱替换保留；未配置时，删除沙箱会丢失本地文件。恢复原生对话状态不会恢复仓库文件。
:::

## 默认仓库字段

Agent 的默认 GitHub 仓库使用以下字段；在**项目代码仓库**中一次性填入：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `url` | string | 是 | SSH 仓库 URL，例如 `git@github.com:your-org/your-repo.git`。 |
| `protocol` | string | 否 | 默认仓库使用 `ssh`，这是当前运行路径支持的协议。 |
| `deploy_key_secret_name` | string | 是 | 保存 SSH 私钥的环境变量逻辑名称。 |
| `branch` | string | 否 | 要克隆的分支或 Tag，省略时使用仓库默认值。 |
| `depth` | integer | 否 | 大于 0 时执行浅克隆，省略时克隆完整历史。 |

第一轮任务开始前，仓库会被克隆到 Session 工作目录。

:::note
Agent 的 `deploy_key_secret_name` 只保存逻辑名称。AstraBox 准备工作树时在服务端读取私钥，Agent API 不会返回私钥。
:::

## 在 Agent 上配置 GitHub 仓库

在控制台中打开 **Agent**，新建或编辑 Agent，然后展开**显示高级设置**。在
**项目代码仓库**中填入：

![创建 Agent 时配置项目代码仓库](img/agent-create-console-zh.png)

```json
{
  "url": "git@github.com:your-org/your-repo.git",
  "protocol": "ssh",
  "deploy_key_secret_name": "your-repo-deploy-key",
  "branch": "main",
  "depth": 1
}
```

保存 Agent，然后从该 Agent 启动新的 Session。Agent 收到第一条消息前，
AstraBox 会完成克隆。

:::tip
请在 Agent 的系统提示词或用户消息中明确任务和目标分支。仓库就是 Session 工作目录，不需要再设置单独的挂载路径。
:::

## 使用多个仓库

一个 Agent 只有一个 `default_repo`。Agent 可以在任务中克隆其他公开 HTTPS 仓库，例如同时分析前端和后端：

```text
把 https://github.com/your-org/frontend 克隆到 ./frontend，
把 https://github.com/your-org/backend 克隆到 ./backend，
然后追踪登录流程在两个仓库之间的调用。
```

提供 Skill 或 Plugin 的仓库需要单独配置为 Skill 来源或 Plugin 仓库，它们不是
额外的项目工作树。

需要不同凭证的私有仓库应使用不同 Agent，或使用经过批准并通过 AstraBox 分配凭证的 Git/MCP 集成。

## 仓库权限模型

默认仓库使用 SSH Deploy Key。每个仓库单独创建 Key；除非 Agent 必须推送，否则只授予读取权限：

```bash
ssh-keygen -t ed25519 -f astrabox-deploy -N ""
```

把 `astrabox-deploy.pub` 添加为 GitHub 仓库的 Deploy Key，并把私钥保存在 AstraBox 服务环境中。逻辑名称会转成大写，短横线会转成下划线，因此 `your-repo-deploy-key` 对应 `YOUR_REPO_DEPLOY_KEY`：

```yaml
services:
  server:
    environment:
      YOUR_REPO_DEPLOY_KEY: |
        -----BEGIN OPENSSH PRIVATE KEY-----
        ...
        -----END OPENSSH PRIVATE KEY-----
```

部分沙箱后端允许 HTTP/HTTPS 出站，但不允许 SSH。对于这些后端，AstraBox 会把克隆转换为 HTTPS，并使用 `ASTRABOX_GIT_HTTPS_TOKEN_SECRET_NAME` 指定的部署 Secret。

### 推荐权限对照

如果 Agent 还使用 GitHub Fine-grained PAT 调用 API 或 `gh`，只授予任务需要的仓库权限。Classic PAT 对应范围更大的 `repo` Scope。

| Agent 操作 | Fine-grained PAT 权限 |
| --- | --- |
| 克隆或读取私有仓库 | `Contents: Read` |
| 创建分支并推送 | `Contents: Read & Write` |
| 创建或评论 Pull Request | `Pull requests: Read & Write` |
| 读取 Issues | `Issues: Read` |
| 创建或评论 Issues | `Issues: Read & Write` |
| 读取仓库元信息 | `Metadata: Read` |

:::tip
Fine-grained PAT 可以限定到具体仓库。优先使用短期、单任务凭证，不要让多个 Agent 共用 Classic PAT。
:::

### 安全建议

1. 不要把私钥或 Token 写入 Agent JSON、日志、截图或代码仓库。Agent 只保存逻辑 Secret 名称。
2. 只有 Agent 必须推送时才授予 Deploy Key 写权限。发生意外泄露后立即吊销或轮换。
3. 开发和生产使用不同凭证，让审计记录保持明确。
4. Environment 使用受限网络时，只允许任务需要的 Git 地址。内置 SSH 克隆
   不会用 known-hosts 校验 Git 服务器提供的 Host Key，因此请使用可信网络
   路径和仓库级凭证。

## 创建 Pull Request 工作流

Agent 可以在仓库目录中直接执行 `git`。创建 Pull Request 还需要 GitHub API 凭证，以及 GitHub MCP 服务器、兼容 API 客户端或安装在所选沙箱镜像中的 `gh` CLI。

要让 Agent 完成“修改 → Push → 创建 PR”全流程：

1. 给仓库 Deploy Key 写权限，并为创建 Pull Request 的 MCP 服务或客户端配置
   GitHub API 凭证。
2. 在用户消息中清晰描述任务、仓库、目标分支和 Pull Request 要求。

例如，在 Session 中发送：

> 修复 issue #128。创建 `fix/refresh-token` 分支，修改
> `src/auth/refresh.ts`，补充测试，提交修改，推送分支，并创建目标为 `main`、
> 标题为 `fix(auth): rotate refresh token on login` 的 Pull Request。

:::note
AstraBox 不会自动把 GitHub PAT 写入 `GH_TOKEN`。请显式配置 GitHub API 凭证，并确认所选 MCP 服务器或 CLI 能够使用它，同时不会把凭证暴露给无关请求。
:::

## 配合 Agent 配置的最佳实践

- 使用 Agent 程序原生的文件、搜索、编辑和命令能力处理代码。
- 在系统提示词或用户消息中写清仓库、目标分支、预期测试和需要保存的产物。
- 长任务结束前让 Agent 执行 `git status`，避免遗漏尚未提交的修改。
- 如需跨 Session 复用产物，请在沙箱终止前推送经过审核的分支，或通过 Session
  文件面板下载 Patch 和报告。

## 常见问题

**Q: 仓库太大，克隆很慢怎么办？**

A: 任务不需要完整历史时，为 `default_repo` 设置大于 0 的 `depth`，并指定分支。只做一次分析时，也可以缩小任务范围或只提供相关文件。

**Q: Deploy Key 或 PAT 过期、被吊销了怎么办？**

A: 新的 clone、push 或 GitHub API 调用会失败。替换服务端 Secret，然后启动新的 Session。如果已有 Session 还有未推送修改，请在释放沙箱前下载 patch。

**Q: 是否支持 fork 私有仓库或访问 organization 内部仓库？**

A: 支持，只要 Deploy Key 或 PAT 可以读取目标仓库。组织对 PAT 强制 SSO 时，必须先授权 PAT，才能用于 GitHub API 操作。

**Q: 是否支持 git submodule？**

A: `default_repo` 没有单独的 submodule 字段。让 Agent 执行 `git submodule update --init --recursive`，并为所有 submodule 仓库提供读取权限。

**Q: Session 运行中能换仓库吗？**

A: 修改 Agent 不会替换当前沙箱中的文件。新的默认仓库、分支或深度用于新工作区，不会替换恢复中的已有工作树。需要独立工作树时，请创建新的 Session。

**Q: Agent 修改的代码会自动推回 GitHub 吗？**

A: 不会。除非 Agent 执行 `git push`，修改只存在于 Session 工作区。请明确要求推送分支或创建 Pull Request。

**Q: GitHub Enterprise Server (GHES) 是否支持？**

A: 使用沙箱能够访问的 SSH 仓库 URL 和该服务器认可的凭证。在 Environment 网络策略中允许对应 Host，并从生产沙箱网络验证 Git 和 API 访问。

## 下一步

- [Session](sessions.md) — 启动和操作 Session
- [HTTP API](api.md) — 创建 Session 并发送任务
- [Skill](agent-skills.md) — 复用代码审查和 Pull Request 流程
- [容器参考](container-reference.md) — 查看工作区目录和文件持久化规则
