# 接入新的沙箱 provider

`SandboxProvider` 为 AstraBox 提供运行 Agent 程序的沙箱：创建沙箱，按 id 重新连接，
续期、暂停和销毁沙箱，并返回一个句柄，用来访问沙箱内的文件、命令、长驻进程和端口。

AstraBox 内置一个 provider：`open_sandbox`，它调用 OpenSandbox 生命周期服务。部署的沙箱
来自其他平台时，再编写新的 provider。

## AstraBox 与 provider 的分工

| AstraBox | provider |
| --- | --- |
| 对话使用哪个沙箱，以及何时创建、暂停或替换沙箱 | 按 `SandboxCreateSpec` 创建沙箱，并按 id 重新连接 |
| Session、工作区规划、凭证，以及启动 Agent 程序 | 销毁沙箱，并证明它已经不存在 |
| 原生对话状态，保存在 AstraBox 数据库中 | 沙箱内的文件、命令、长驻进程和端口地址 |
| 拒绝后端没有声明支持的配置 | 声明后端能运行什么，并实现所声明的能力 |

完整契约及其中的每个类型见 `astrabox/seams/sandbox.py` 和
`astrabox/seams/sandbox_data.py`。

## 返回句柄

`create_sandbox` 和 `connect` 返回一个 `SandboxHandle`。AstraBox 只使用它的以下成员：

| 成员 | 契约 |
| --- | --- |
| `sandbox_id` | provider 之后重新访问该沙箱所用的 id。 |
| `files` | `SandboxFiles`：绝对路径按字面使用，权限使用 POSIX 权限位（例如 `0o640`），属主使用账户名，失败时抛出 `SandboxPathNotFound`、`SandboxPathRejected` 或 `SandboxUnreachable`。 |
| `commands` | `SandboxCommands`：以 root 运行一条 shell 命令行，stdin 为空。非零退出码作为数据放在 `SandboxCommandResult` 中；超过截止时间时抛出 `TimeoutError`，沙箱随后停止该命令。 |
| `processes` | `SandboxProcesses`：长驻进程（例如终端的 shell 或 Pi 程序），任一副本都可以按 id 接入。同一时刻只有一个接入方持有进程，另一个接入方可以接管。输出按字节编号，stdout 和 stderr 合在一起；可以从沙箱仍保留的偏移重放，否则抛出 `SandboxProcessReplayGap`。已退出的进程会在下一个接入方到来时再次运行。 |
| `get_endpoint(port)` | 一个 `SandboxEndpoint`：能到达沙箱内该端口的绝对 `http` 或 `https` URL，以及每个请求都必须携带的请求头。 |
| `get_signed_endpoint(port, expires_at=...)` | 一个 `SandboxEndpoint`，其 URL 本身携带有效期到 `expires_at` 的凭证，供无法发送请求头的客户端使用。无法签名的后端抛出 501 `SANDBOX_SIGNED_ENDPOINT_UNSUPPORTED`。 |

沙箱无法保持长驻进程的 provider 将 `supports_process_sessions` 保持为 false，并从
`processes` 返回 `UnsupportedSandboxProcesses(name)`；对它的每个操作都会抛出 501
`SANDBOX_PROCESS_SESSIONS_UNSUPPORTED`。句柄若提供 `close()`，它只释放本地资源，
从不销毁沙箱。

## 复用 execd 实现文件、命令和进程

每个内置沙箱镜像都在 `/opt/astrabox/opensandbox/execd` 带有 OpenSandbox 的 execd
`v1.1.0`（见[容器参考](container-reference.md)）。如果 provider 的沙箱在端口 `44772`
上运行 execd，且 `get_endpoint(44772)` 返回能到达它的 URL 和请求头，就可以直接用
`astrabox.providers.execd` 构建句柄的三个数据面，而不必自己编写：

```python
from astrabox.providers.execd import EXECD_PORT, ExecdCommands, ExecdFiles, ExecdProcesses


async def data_plane(sandbox_id, get_endpoint):
    execd = await get_endpoint(EXECD_PORT)
    return (
        ExecdFiles.at_endpoint(execd.url, execd.headers, sandbox_id=sandbox_id),
        ExecdCommands.at_endpoint(execd.url, execd.headers, sandbox_id=sandbox_id),
        ExecdProcesses.at_endpoint(execd.url, execd.headers, sandbox_id=sandbox_id),
    )
```

这些实现已经处理 execd 的线协议细节：权限位格式、退出码的报告方式、重放帧和连接关闭方式。
使用它们时，provider 声明 `supports_process_sessions = True`。

execd 以 root 运行，会执行任何到达它的请求。请在 `get_endpoint` 返回的请求头中放入
每个沙箱自己的凭证，并确保沙箱内的工作负载读不到它；或者只允许 AstraBox 访问该端口。

provider 也可以用所在平台自己的 API 实现这三个协议。这时必须遵守同样的契约，包括在同一次
操作中写入文件的权限位和属主，因此请用下文的一致性套件验证。

## 声明后端能力

provider 类上的能力标志说明后端支持什么。注册时，声明为 true 的标志必须同时具备它在
`SANDBOX_CAPABILITY_RULES` 中那一行要求的方法，否则会被拒绝；因此只声明了一半的能力
会在安装 provider 时失败，而不是等到第一段需要它的对话才失败。

| 标志 | provider 还需要实现 |
| --- | --- |
| `supports_create_network_policy` | `admit_runtime_egress`；按 Environment 的网络访问规则创建每个沙箱 |
| `supports_isolated_sessions` | 隔离会话的各项操作和 `count_active_isolated_sessions`；`read_memory_headroom` 和 `read_listening_ports` 有默认实现，通过 `commands` 读取沙箱 |
| `supports_isolated_session_recovery` | `find_isolated_sessions_by_workspace` |
| `supports_pause` | `pause` 和 `resume` |
| `supports_client_pool` | 客户端池的各项操作、`owns_unclaimed_sandbox` 和 `client_pool_readiness` |
| `supports_correlated_create` | `find_sandbox_by_assignment` |
| `supports_egress_credential_injection` | `apply_credential_vault` |
| `supports_inventory_listing` | `list_sandboxes` 和 `describe_sandbox` |
| `supports_turn_preparation` | `prepare_turn` 和 `owns_sandbox` |
| `supports_process_sessions` | 句柄的 `processes` |

保存配置时，如果所选后端没有声明该配置需要的能力，AstraBox 会拒绝保存：后端没有
`supports_create_network_policy` 时的任何 Environment；没有 `supports_isolated_sessions`
时的沙箱共享；没有 `supports_pause` 时的 `Pause` 空闲行为；客户端池未就绪时的预热容量；
以及不共享沙箱时，没有 `supports_process_sessions` 的 Pi Agent 程序或 Agent 终端面板。

`supports_correlated_create` 决定建箱途中平台重启后能恢复什么。声明了它，AstraBox 会用
`find_sandbox_by_assignment` 找回被中断的那次启动可能建出的沙箱并继续使用。没有声明，
AstraBox 每个沙箱只建一次、从不查询：建箱抛错时，这次对话启动就以该错误失败；被平台重启
中断的启动会以 `409 SANDBOX_CREATE_INTERRUPTED` 拒绝，而不会再建第二个沙箱，由用户重新发起
对话。后端为那次被中断的启动建出的沙箱，AstraBox 不知道它的存在，由后端自己的过期机制回收。
传输中失败的请求是否重试，由 provider 自己的客户端负责，且只对后端能接受重复提交的请求重试。

## 证明沙箱的去向

只有后端确认这个沙箱已经不存在时，`kill(sandbox_id)` 才返回 `True`；接受了删除请求但
没有确认结果时返回 `False`。AstraBox 从不直接调用它，而是调用 `confirm_destroyed`，
把这个结果和 `probe` 的第二次观察一起变成结论。无法探测的后端从 `probe` 抛出
`NotImplementedError`；其他任何异常都视为一次失败的探测，销毁结果会保持未确认。

`endpoint_names_sandbox(endpoint)` 返回该后端的端点字符串中编码的沙箱 id；地址里没有
这个信息时返回 `None`。只有后端确认已保存的端点指向预期的沙箱时，AstraBox 才使用它，
否则按沙箱 id 重新解析。

## 启用后端

`ASTRABOX_SANDBOX_BACKENDS` 列出部署启用的后端，`ASTRABOX_SANDBOX_BACKEND` 指定未选择
后端的 Environment 使用的默认后端。仍指向已安装但未启用后端的记录会以
`SANDBOX_BACKEND_DISABLED` 失败。把只有该 provider 读取的环境变量列在 `owned_settings`
中：provider 已安装但未启用时，AstraBox 会在启动阶段对其中每个已设置的变量发出警告。

能挂载持久工作区存储的后端从 `workspace_route()` 返回一个 `WorkspaceRoute`。没有它时，
为该后端配置了工作区卷的部署会在启动时被拒绝。

## 打包 provider

在 provider 的 Python 发行包中声明它：

```toml
[project.entry-points."astrabox.providers.sandbox"]
example = "astrabox_sandbox_example:ExampleSandboxProvider"
```

把类的 `name` 设为入口点名称。AstraBox 启动时会实例化并注册这个类。类也可以设置
`seams_api_version`；声明的版本不兼容时，服务在启动阶段失败，而不是等到第一段对话。

## 验证 provider

在 provider 的单元测试中绑定结构契约套件：

```python
from astrabox.testing.provider_conformance import SandboxProviderContractSuite

from astrabox_sandbox_example import ExampleSandboxProvider


class TestExampleSandbox(SandboxProviderContractSuite):
    def make_provider(self):
        return ExampleSandboxProvider()
```

它检查抽象方法都已实现、能力标志都是布尔值，并且每个已声明的能力都具备规则要求的内容。

然后在针对该 provider 真实沙箱的在线测试中绑定数据面套件，每个条款一个用例：

```python
import pytest

from astrabox.testing.sandbox_data_conformance import (
    DATA_CONTRACT,
    PROCESSES_CONTRACT,
    DataContractBox,
    run_contract_check,
)


@pytest.mark.parametrize("check", DATA_CONTRACT + PROCESSES_CONTRACT, ids=lambda check: check.name)
async def test_data_contract(check, live_handle):
    await run_contract_check(check, DataContractBox(
        files=live_handle.files,
        commands=live_handle.commands,
        scratch="/tmp/data-contract",
        account="agent",
        processes=live_handle.processes,
    ))
```

没有 `supports_process_sessions` 的 provider 去掉 `PROCESSES_CONTRACT`，也不传
`processes`。`account` 是沙箱镜像中存在、带有同名用户组、且不是文件面自身运行身份的账户。

最后，在启用该 provider 的真实部署中走一遍对话：开始一段对话，在文件面板中读写文件，
在终端中运行一条命令并中断它，等沙箱空闲后再发送一条消息。

## 相关文档

- [架构](architecture.md)：沙箱 provider 所处的位置
- [OpenSandbox 后端](providers/opensandbox.md)：内置 provider
- [容器参考](container-reference.md)：沙箱镜像中带有什么
- [嵌入 AstraBox](embedding.md)：入口点与启动
