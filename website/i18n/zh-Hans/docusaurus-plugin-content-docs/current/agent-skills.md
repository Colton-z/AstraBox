# Agent Skills

> 为 Agent 附加领域专业知识。

Skills 为 Agent 附加**领域专业知识**。一个 Skill 是一组结构化的提示词和流程，让 Agent 在特定任务上表现得更专业、更可靠。

打开**管理台 → Agent**，创建或打开一个 Agent，然后在 **MCP、Skill 与 Plugin**区域选择 Skill。

![创建 Agent 时选择 Skill](img/agent-create-console-zh.png)

## 添加方式

| 方式 | 位置 | 说明 |
| --- | --- | --- |
| 目录 Skill | 部署启用[扩展目录](adding-tools.md#extension-catalog)时，Agent 详情页的「Skill 配置」 | 从部署的目录中选择可复用的 Skill |
| 直接使用 Git 来源 | Agent 创建或编辑页的「MCP、Skill 与 Plugin」 | 添加仓库 URL，以及可选的版本和子目录 |
| 结构化来源 | 同一个 Skill 字段 | 使用经过批准的 deploy key 配置 SSH Git，或使用部署安装的 provider 提供的内容 |

直接配置的 Skill 字段是一个 JSON 数组，元素为 Git 字符串或来源对象。对象格式、SSH 凭证以及安装非 Git provider 的方法，见 [Skill 内容 provider](providers/skill-content.md)。

## 版本化模型

Git Skill 使用代码仓库和可选版本：

- **Skill 来源**：仓库 URL 加可选的 `@ref` 和 `#path`，例如 `https://github.com/example/skills.git@main#skills/code-review`。
- **固定版本**：Skill 不应在修改 Agent 之前发生变化时，使用经过审核的 Commit SHA。
- **未固定版本**：省略 ref 时跟随仓库默认分支；填写分支或 Tag 时，准备运行实例时跟随对应 ref。
- **目录 Skill**：目录为每个条目提供来源，Agent 保存选择结果。

结构化来源会明确写出 provider 和版本。provider 先把该版本解析为不可变的内容，AstraBox 再检查缓存；每次准备都会检查访问权限，命中缓存时也不例外。

AstraBox 不会创建另一套 Skill 内容格式，而是把 Skill 准备到所选 Agent 程序使用的原生 Skill 目录中。

私有 HTTPS 来源需要平台管理员在下载前配置 **HTTP Basic（Git HTTPS）** 凭证，并将
Vault 分配给 Agent。凭证的目标地址只填写仓库 URL，`@ref` 和 `#path` 保留在 Skill
来源中。直接来源和目录 Skill 都适用，具体操作见
[私有 Git 仓库鉴权](credentials.md#private-git-repositories)。HTTP(S) URL 里带用户名或令牌的
Skill 来源会被拒绝：来源会随 Agent 保存，并在下载错误里原样出现；Vault 凭证则在沙箱外注入，
不会随 Agent 保存。

## Skill 的作用

- **注入专业知识** —— 让通用 Agent 具备特定领域能力（如代码审查、文档生成）
- **标准化流程** —— 确保 Agent 按统一步骤执行，输出一致
- **可复用** —— 一次创建，多个 Agent 共享

## Skill 文件结构

Skill 仓库中包含一个以 `SKILL.md` 为核心的目录：

```
my-skill/
├── SKILL.md          # 必需：Skill 定义文件
├── templates/        # 可选：模板文件
│   └── report.md
└── examples/         # 可选：示例文件
    └── sample.json
```

`SKILL.md` 是核心文件，使用 YAML frontmatter + Markdown 格式：

```markdown
---
name: my-skill
description: 执行结构化代码审查，输出改进建议
---

# Code Review

## Steps
1. 分析代码结构和架构
2. 检查常见问题（安全、性能、可维护性）
3. 输出结构化审查报告

## Pitfalls
- 不要只关注格式问题，优先关注逻辑错误
- 给出具体修改建议，而非泛泛批评
```

## 创建 Skill

在 Git 仓库中创建 Skill 目录并提交完整内容，记录仓库 URL、版本和子目录。部署安装的内容 provider 也可以从其他来源提供带版本的 Skill 目录。AstraBox 没有单独的 Skill zip 上传接口。

目录的 provider 提供管理页面时，Agent 详情页的「在 … 中管理 Skill」会打开它，可以用它把 Skill 加入目录。

## 关联到 Agent

在网页控制台中打开 Agent。要使用目录 Skill，在「Skill 配置」中选择并保存；要使用直接来源，在「MCP、Skill 与 Plugin」的 Skill 字段中，把它的 Git 字符串或来源对象加入 JSON 数组，然后保存该部分。直接来源不需要扩展目录。

## 版本管理

对于 Git Skill，提交修改；需要使用该版本时，更新 Agent 的 Git ref 或目录条目。对于其他来源 provider，通过该提供方发布，再更新来源对象中的版本。已经运行的 Agent 程序不会被原地改写；AstraBox 首次准备或重建 Session 运行实例时，会读取 Agent 当前配置的 Skill 来源。

启用预热时，Skill 可能在 Session 启动前就已准备好。在 Agent 上使用「重新预热」，
可按已保存的 Skill 来源重新拉取并替换待领取资源，不中断现有 Session；固定的
Commit 版本不会自动升级。

## 获取 Skill 详情

打开 Agent 详情页。直接来源显示在「MCP、Skill 与 Plugin」的 Skill 字段中；已经选择的目录 Skill 显示在旁边的目录选择框中。

## 列出所有 Skills

打开 Agent 详情页。「Skill 配置」中的目录选择框会列出该 Agent 可以使用的目录 Skill。provider 提供管理页面时，旁边的「在 … 中管理 Skill」按钮会打开它。

## Skill 编写建议

1. **明确触发条件** —— 在 description 中写清楚何时应使用此 Skill
2. **步骤具体** —— Steps 中写精确操作，而非模糊描述
3. **记录陷阱** —— Pitfalls 帮助 Agent 避免常见错误
4. **提供验证** —— 告诉 Agent 如何确认任务完成

## 常见问题

**Q：Skill 和 Agent `system` 提示词有什么区别？**

A：`system` 是 Agent 的通用提示词，对所有任务生效。Skill 是按需激活的专业模块，Agent 根据任务内容决定是否使用。

**Q：一个 Agent 可以关联多少个 Skills？**

A：无硬性限制，但建议控制在 10 个以内以确保 Agent 行为可预测。

**Q：哪些 Agent 程序支持 Skills？**

A：支持情况由所选 Agent 程序声明。如果程序不能使用 Skill，AstraBox 会拒绝保存配置。

**Q：Skill zip 文件有大小限制吗？**

A：AstraBox 不通过 zip 上传 Skill。运行实例中加载的内容受来源 provider 和沙箱限制。

## 下一步

- [Agent 工具与扩展](adding-tools.md) — 配置 MCP 服务器、Skill 和 Plugin。
- [定义 Agent](authoring-agents.md) — 查看 Agent 配置。
- [Session](sessions.md) — 使用 Agent 开始工作。
- [快速入门](quickstart.md) — 通过五个步骤运行第一个 AstraBox Agent。
