# 从 MCP 客户端使用 AstraBox Agent

AstraBox 通过一个远程 MCP Server 提供当前身份有权使用的 Agent。连接一次后，MCP 客户端
就可以启动 Agent、查看结果、在 Agent 请求输入时作答，以及停止正在执行的任务。

| 项目 | 值 |
|---|---|
| 传输方式 | Streamable HTTP |
| Server 地址 | `<AstraBox 地址>/api/v1/mcp` |
| 认证方式 | AstraBox MCP 客户端密钥、部署接受的身份服务 Token，或本地单用户模式 |

所有兼容客户端都连接到同一个远程 MCP Server。不同的只是客户端使用什么配置语法，以及
把配置保存在哪里。

![MCP 客户端如何访问 AstraBox Agent](./img/agent-mcp.svg#inline)

## 连接客户端

1. 打开**管理台 → MCP 客户端**。
2. 选择**签发密钥**，用存放密钥的机器或客户端命名，再设置密钥 Scope。
3. 离开页面前复制生成的 MCP 配置。配置中已经包含当前部署的 Server 地址和刚签发的密钥。
4. 把整段内容粘贴到客户端的远程 HTTP MCP 配置中，再在客户端检查连接。

密钥只显示一次。包含密钥的配置应保存在用户范围，并排除在版本控制之外。如果密钥丢失，
请吊销它并重新签发。

## 密钥 Scope

| Scope | 允许的操作 |
|---|---|
| **仅读取** | 发现经过鉴权可以使用的 Agent，并读取已有任务的状态。 |
| **读取并对话** | 读取、启动任务、发送输入、回答交互，以及取消正在执行的任务。 |

MCP 客户端密钥同时携带签发用户的身份和这里选择的 Scope。AstraBox 还会在每次工具调用时
执行当前的 Agent 鉴权，因此吊销密钥或修改 Agent 鉴权设置都会从下一次请求开始生效。

OIDC 部署也可以接受已配置身份服务签发的 Access Token。JWT 部署接受通过签发者、签名、
受众和过期时间检查的 Token。本地单用户模式的本机连接可以不带 `Authorization` 请求头。

## 运行任务

MCP 客户端会直接从 Server 发现可用操作及其输入。一次任务通常经过以下过程：

1. 客户端找到当前身份有权使用的 Agent。
2. `create_conversation` 创建新的 AstraBox Session，并返回 `session_id` 和页面地址。
3. `send_message` 提交任务后立即返回，不会让一次 MCP 调用持续到整个任务结束。
4. 客户端通过 `get_status` 读取当前状态和最近消息。
5. 状态为 `WAITING_INPUT` 时，客户端发送答案并继续检查状态；状态为 `READY` 时，读取结果
   或打开 Session 页面。

`create_conversation` 虽然使用了 conversation 这个名称，创建的资源仍是 AstraBox Session；
返回的 `session_id` 也不是 MCP 传输层 Session ID。Session 进入 `TERMINATED` 或 `DELETED`
后，需要创建新的 Session。

## 连接如何工作

该地址提供无状态的 Streamable HTTP MCP Server。MCP 客户端负责初始化、协议版本协商、
工具发现和 JSON-RPC 消息。`send_message` 在 AstraBox 接受任务后就会返回，因此任务可以在
云端继续执行，不依赖一条持续打开的工具调用。

把 Server 地址写在笔记本电脑上的配置文件中，并不会让它变成本地 MCP Server。Server
仍然是远程的，只是配置保存在本地。

## 相关指南

- [运行 Session](sessions.md)
- [设置团队登录](team-login.md)
- [为 Agent 配置 MCP Server、Plugin 和 Skill](adding-tools.md)
