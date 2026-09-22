# 容器参考

AstraBox 的 Environment 会让每个 Agent 在容器中运行。AstraBox 是自托管软件，
因此操作系统、命令和资源由沙箱镜像与部署配置决定。

在 AstraBox 自带的 Agent 镜像中，可以稳定依赖以下行为：

- Agent 程序默认从 `/workspace` 启动；
- 每个沙箱都需要的软件会预先构建到镜像中；
- Session 启动时，AstraBox 不会执行通用 setup script；
- 工作区文件的保留取决于沙箱生命周期和可选的持久存储。

## 运行时与操作系统

AstraBox 不保证所有 Agent 镜像都使用同一个操作系统版本。CPU 架构、内核和
容器运行时也可能因部署而异。

在 Session 中运行以下命令可以查看实际环境：

```bash
cat /etc/os-release
uname -m
uname -r
```

> 需要运行原生二进制时，请在 Session 中检测实际架构，或同时提供所需架构的
> 构建产物。

## 当前镜像中的工具

每个沙箱镜像都包含所选 Agent 程序，以及它在 AstraBox 中运行所需的服务。具体
的系统命令和语言版本由镜像决定，并且可能随镜像更新而变化。

请在 Session 中检查任务依赖的版本：

```bash
git --version
python3 --version
node --version
```

任务依赖精确版本时，请在自定义镜像中固定版本，并使用不可变的镜像标签或
digest。

## 工作目录

Agent 程序的默认工作目录是：

```text
/workspace
```

AstraBox 自带的 Agent 镜像将 `WORKSPACE` 设置为 `/workspace`，未指定其他目录
时，命令也从这里执行。代码仓库、上传文件和生成结果都放在这个工作区中。

Session 的文件面板和 API 直接操作这个工作区；AstraBox 不会另外创建 File
资源，也不使用 `mount_path`。参见[文件](files.md)。

## 安装额外软件

每个 Session 都需要的软件应添加到沙箱镜像中。请从所选 Agent 程序对应的
AstraBox 镜像开始构建，标签与你运行的 AstraBox 版本相同：

```dockerfile
FROM ghcr.io/colton-z/astrabox-sandbox-claude-code:0.1.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client redis-tools \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir 'pandas==2.2.3'
RUN npm install -g 'typescript@5.8.3'
```

| 包管理器 | 安装方式 |
| --- | --- |
| apt | 通过 `apt-get install` 安装系统依赖 |
| pip | 通过 `pip3 install` 安装 Python 包 |
| npm | 通过 `npm install -g` 安装 Node.js 包 |

构建完成后，在 Environment 控制台的 **沙箱镜像或模板** 中选择这个镜像。
生产环境应固定基础镜像和依赖版本。

Session 启动时，AstraBox 不会安装通用依赖列表，也不会执行通用 setup script。
沙箱需要的系统账号、Agent 程序和后台服务也必须预先构建到镜像中，确保按需创建
和预热的沙箱行为一致。

## 资源与超时

AstraBox 当前的 OpenSandbox 创建配方为每个沙箱设置 `4` CPU 和 `4Gi` 内存的运行上限，
以及 `200m` CPU 和 `768Mi` 内存的调度请求。冷创建与预备容量使用同一份配方。这些值是
平台设置，不是单个 Session 的 Environment 选项；磁盘容量和执行期限仍取决于沙箱服务
与部署。

如果任务存在最低资源要求，请先在目标 Environment 中验证。内存或磁盘耗尽时，进程
可能被终止或写入失败；长任务还应考虑部署配置的执行期限。

## 文件持久化

- 同一个沙箱存续期间，文件会在 Turn 之间保留；
- 未配置持久工作区存储时，“终止”会在闲置期限结束后删除沙箱及其本地工作区；
- 配置持久工作区存储后，同一段对话的工作文件可在沙箱替换后保留，配置见[部署](deploy.md)；
- “暂停”只在所选沙箱服务支持并验证快照时保存沙箱文件系统；下一个 Turn 会
  先恢复这些文件，再继续运行 Agent 程序。恢复的是文件，不是进程内存；挂载的持久工作区
  存储有独立的生命周期；
- 没有快照或持久工作区时，重新创建沙箱不会保留先前的本地工作文件。

原生对话状态单独保存在 AstraBox 数据库中。恢复这些状态不依赖持久工作区存储卷。

> 容器文件系统是工作目录。重要文件应通过 Session 文件面板或 API 下载，代码
> 修改应 commit 并 push，需要长期保留的结果应写入其他外部存储。

## 执行用户与环境变量

执行用户以及 `HOME`、`USER`、`SHELL` 和 `LANG` 的值可能因 Agent 程序、沙箱
使用方式或自定义镜像而异。不要让脚本依赖特定 UID，也不要假设系统目录始终
可写。

需要确认时，在 Session 中运行：

```bash
id
whoami
printf 'HOME=%s\nUSER=%s\nSHELL=%s\nLANG=%s\nWORKSPACE=%s\n' \
  "$HOME" "${USER:-}" "${SHELL:-}" "${LANG:-}" "${WORKSPACE:-}"
```

模型凭证和关联 Credential Vault 中的凭证会根据 Environment 与 Credential
Vault 配置提供给 Agent 程序。不要在日志或任务输出中打印密钥。

## 相关文档

- [运行环境](environments.md) - Environment 配置
- [文件](files.md) - 操作 Session 工作区中的文件
- [Credential Vault](credentials.md) - 凭证与注入
