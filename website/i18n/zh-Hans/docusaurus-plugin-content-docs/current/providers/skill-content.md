# Skill 内容 provider

Agent 和 Assistant 使用同一个 `skills` 数组。每个条目是一个 Git 字符串
（`repository@ref#directory`）或一个来源对象。控制台以 JSON 形式编辑这个数组。扩展
目录 provider 可以在 `ExtensionRuntimeSelection.skill_descriptors` 中返回任一形式。

```json
{
  "provider": "git",
  "source": "git@code.example.com:team/skills.git",
  "version": "0123456789abcdef0123456789abcdef01234567",
  "path": "skills/example",
  "name": "example",
  "config": {
    "deploy_key_secret_name": "skills-read-key",
    "known_hosts": "code.example.com ssh-ed25519 <verified-public-host-key>"
  }
}
```

对象中的 `provider`、`source`、`version` 和 `name` 为必填。`path` 默认为来源的根
目录。所选目录必须包含 `SKILL.md`。同一个声明中，安装后的名称必须唯一。路径不能超出
来源范围；符号链接必须解析到所选目录之内。发布时会复制其内容，保留可执行文件，并去掉
特殊权限位。

## Git 与 SSH

公开 Git 和私有 HTTPS Git 使用同一个内置的 `git` provider。私有 HTTPS 凭证保存在
Agent 的 HTTP Basic Vault 凭证中；URL 中不能包含凭证。Git 先把 ref 解析为完整的提交，
再计算内容缓存标识。为了可复现，建议使用完整的提交 ID。有歧义的分支或标签名会被拒绝；
请使用完整的 `refs/heads/…` 或 `refs/tags/…` 名称。提交的短名称需要检出后才能解析，
即使在查询缓存之前也是如此。

SSH 同时需要 `config.deploy_key_secret_name` 和 `config.known_hosts`。把密钥的逻辑
名称加入 `ASTRABOX_DEPLOY_KEY_SECRET_NAMES`，并通过已有的具名环境机密机制提供私钥值。
读取机密之前会先检查这份允许列表。通过可信渠道向仓库运营方获取主机公钥；仅靠
`ssh-keyscan` 无法验证它的真实性。SSH 端口不是标准端口时，使用 OpenSSH 的
`[hostname]:port` known-hosts 格式和 `ssh://` 来源 URL。

获取过程以沙箱的平台账户（root）运行。密钥和 known-hosts 文件保存在工作区之外一个
独立的、只有 root 可以访问的目录中，通过沙箱文件 API 以 `0600` 权限写入。SSH 只使用
这个身份，禁用认证代理（authentication agent）和环境中已有的 SSH 配置，并要求验证
主机密钥。无论成功、失败还是取消，核心都会删除临时材料。只支持 HTTPS Git 的沙箱会拒绝
SSH；不会退回到其他协议或身份。

## 安装其他 provider

在部署自己的包中实现 `astrabox.seams.skills.SkillContentProvider`，并在
`astrabox.providers.skills` 入口点下声明这个类。启动服务前，把这个包安装到服务镜像中。
provider 在沙箱中执行的任何工具都必须已经包含在该沙箱镜像中。Agent 配置中不能指定
Python import 或安装命令。

provider 负责四项操作：

- `validate(source)` 检查其配置和已批准的凭证名称，不读取机密，也不访问网络。遇到
  未知选项时应拒绝。
- `egress_hosts(source)` 声明获取内容时沙箱需要访问的主机。核心会对这些主机应用已有的
  Agent 作者/RuntimeProfile 网络访问规则。
- `resolve(source, sandbox, private_dir)` 授权访问，并返回
  `ResolvedSkillSource(version, content_id)`。两个字段都是公开、不可变的标识。如果
  供应方的标识只在某个租户或命名空间内唯一，请把租户或命名空间包含在来源、配置或内容
  标识中。
- `fetch(source, resolved, sandbox, destination, private_dir)` 在最初并不存在的目标
  目录中准备解析出的那个确切版本。provider 可以使用供应方的 SDK 或镜像中的工具。它不负责
  安装到缓存或对话中。

每次准备都会执行解析，包括命中缓存时。解析必须使用当前凭证对请求的来源进行授权。命中
缓存时会跳过获取，但不会跳过授权。临时凭证只能放在提供的私有目录中。核心会删除这个
目录；provider 负责关闭自己的连接和其他外部资源。使用基类的 `write_credential`，在
传播取消之前先完成正在进行的文件写入，这样清理就不会与迟到的凭证写入发生竞争。
不要把凭证放在命令文本、命令环境变量、返回值、日志、异常或来源内容中。

核心按 provider、来源声明、解析出的版本和内容标识为内容建立键。独立的暂存目录隔离
并发的获取；未命中缓存时可以并发获取。文件系统锁使发布串行进行。完整的目录树会原子地
重命名到其不可变的缓存位置，然后原子地替换其声明链接。对话准备使用该声明对应的目录。
获取失败时不会发布完成标记。在发布之前取消会阻止发布；原子提交开始之后，如果响应丢失，
就无法确定是否已经提交。无论哪种情况，可见的目录树都是完整的。无法访问的沙箱无法确认
凭证已经清理，这种失败会如实报告，而不会报告为清理成功。

未知的 provider 会明确报错。缓存目录树和临时文件位于用户工作区之外。替换沙箱后，会按
同样的声明和授权流程重新获取内容。

## 设计参考

来源接口的划分对照了 AstraBox `3910d23b`（已有的 ExtensionProvider、引擎安装脚本、
Git 凭证解析器和扩展包缓存的文件系统锁）以及 Vercel Skills
[`13e4063a`](https://github.com/vercel-labs/skills/tree/13e4063a1cf913f5606d57d42ab83a86f5001e04/src/providers)。
Vercel 的 provider 注册表采用 MIT 许可并持续维护，它把远程内容获取与安装分开，但它面向单个
SKILL.md 的宿主接口不提供本运行平台所需的沙箱凭证生命周期、目录树或不可变版本要求。
把这个 TypeScript CLI 作为服务端依赖会再引入一个运行时，而这些适配层照样需要。没有
复制任何代码。AstraBox 保留已有的文件系统锁和具名机密机制，并补充了它们没有提供的内容
来源接口。

Git ref 解析遵循官方的 [ls-remote 约定](https://git-scm.com/docs/git-ls-remote)，
包括解引用（peeled）的附注标签。SSH 身份与主机验证遵循
[OpenSSH 配置](https://man.openbsd.org/ssh_config)。
