# 在 Python 应用中使用 AstraBox

大多数应用会把 AstraBox 作为独立服务运行，再调用它的 HTTP API。确实需要共用进程的
Python 应用也可以通过 `create_app()` 加载 AstraBox 的 ASGI 应用。专用 Python worker
还可以直接使用服务层，但必须自行管理所需的每一项生命周期工作。

前两种方式提供相同的 Agent、Session、流式响应、Deployment、凭证和沙箱生命周期。
进程内使用不会把 Agent 程序放进原有应用进程；Agent 程序仍在 OpenSandbox 沙箱中运行，
开发者关闭自己的电脑后也可以继续工作。

## 可用的集成方式

| | HTTP 服务 | 原有进程中的 ASGI 应用 |
|---|---|---|
| 可用语言 | 任意语言或框架 | Python 与 ASGI Server |
| 部署方式 | AstraBox 独立部署和扩缩容 | AstraBox 与原有应用一起部署 |
| 故障与依赖隔离 | 独立进程和 Python 环境 | 共用进程、依赖、事件循环和故障 |
| 访问方式 | HTTP、SSE、WebSocket 和远程 MCP | 通过 ASGI 应用提供相同路由 |
| 扩展方式 | 安装 AstraBox 扩展包 | 安装扩展包，也可以增加最外层 ASGI Middleware |
| 升级方式 | 单独升级 AstraBox | 一起测试和升级组合后的应用 |

应用只需要创建或使用 Agent 时，HTTP 服务即可满足需要。只有在共用 Python 进程或直接
组合 ASGI 应用是明确要求时，才需要加载 ASGI 应用。

## 将 AstraBox 作为 HTTP 服务运行

先在回环地址启动应用：

```bash
astrabox serve --host 127.0.0.1 --port 8088
```

AstraBox 进程会准备存储、执行数据库迁移、启动后台恢复与计划任务、报告就绪状态，并在
关闭时排空正在执行的工作和释放连接。调用方只需要完成认证并使用公开 API。

应用接口见 [HTTP API](api.md)、[API 认证](api-authentication.md)和
[事件流](events-stream.md)。需要让服务离开单台可信主机时，参见
[部署 AstraBox](deploy.md)。

## 加载 ASGI 应用

`create_app()` 返回 `astrabox serve` 使用的 FastAPI 应用：

```python
from astrabox.api.app import create_app

astrabox_app = create_app()
```

请在调用 `create_app()` 前设置 AstraBox 环境变量。创建对象时会注册路由和 Middleware，
但不会连接数据库或沙箱服务；这些操作从应用 lifespan 启动时开始。

ASGI Server 可以直接运行该 factory：

```bash
uvicorn astrabox.api.app:create_app --factory --host 127.0.0.1 --port 8088
```

该命令使用相同的启动入口，但不会与另一个应用共用进程。需要一个组合进程时，请把
AstraBox 挂载到上层 FastAPI 应用的根路径，并由上层 lifespan 进入 AstraBox lifespan：

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from astrabox.api.app import create_app

astrabox_app = create_app()


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with astrabox_app.router.lifespan_context(astrabox_app):
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/host-health")
async def host_health() -> dict[str, str]:
    return {"status": "ok"}


# 先注册原有应用的路由，其余根路径由 AstraBox 处理。
app.mount("/", astrabox_app)
```

FastAPI 只会自动运行主应用的 lifespan，不会自动运行挂载子应用的 lifespan。必须像上例
一样准确进入和退出一次 AstraBox lifespan，否则数据库迁移、后台恢复、计划任务和关闭
流程都不会执行。

## 将 AstraBox 保持在域名根路径

AstraBox 管理台使用 `/assets`、`/api` 和 `/manage` 等根路径下的静态资源、API、认证和
管理台地址。因此，不支持把应用挂载到 `/astrabox` 这样的子路径。

上层应用可以在根挂载之前注册自己的路由。如果两个应用都需要各自的根路由或浏览器
界面，请为 AstraBox 使用独立域名，或将单独的虚拟主机转发给 ASGI 应用。
内置管理台不支持部署在路径前缀下。

## Lifespan 启动的服务

AstraBox lifespan 管理完整的服务生命周期：

- 加载已经安装的扩展和 Provider（扩展实现）；
- 准备数据库并执行尚未运行的迁移；
- 启动定时 Deployment 和运行恢复；
- 在核心服务启动后进入已经安装的 lifespan hook；
- 关闭前将 `/readyz` 切换为未就绪；
- 排空正在执行的工作并释放运行连接。

必要扩展、数据库准备、迁移和定时 Deployment 必须成功初始化。运行恢复服务的启动
错误会写入日志，但不会中止应用启动；首个 API 请求可以再次尝试初始化。
Lifespan 成功进入之前，上层应用不应接受请求。

## 不使用 HTTP，直接调用 AstraBox 服务

不需要管理台或 HTTP 路由的 Python worker，可以明确完成 Provider 组合、存储准备、
数据库迁移和运行恢复：

```python
from astrabox.bootstrap import bootstrap
from astrabox.persistence.migrations import run_pending_migrations
from astrabox.persistence.repository import backend
from astrabox.core.service.orchestrator.service_registry import (
    get_platform_service,
    run_lifecycle_shutdown,
    run_lifecycle_startup,
)

bootstrap()
await backend.create_all()
await run_pending_migrations()
await run_lifecycle_startup()

platform = get_platform_service()

try:
    # 使用明确的用户身份调用所需服务。
    ...
finally:
    await run_lifecycle_shutdown(reason="host_shutdown")
```

`bootstrap()` 可以重复调用。它会加载内置和已安装的 Provider、选择已经配置的沙箱服务，
并拒绝不兼容的部署设置。它不会准备存储、执行迁移、启动定时 Deployment、进入应用
lifespan hook 或安排关闭流程。

上例只启动服务层使用的运行恢复，不能代替完整应用 lifespan。原有应用需要定时
Deployment、应用扩展、就绪与排空、管理台或任何 HTTP 能力时，请使用 `create_app()`。
直接调用服务时仍要传入明确的用户身份，正常鉴权不会因为共用进程而消失。

## 扩展应用

已经安装的 Python 包可以通过 entry point 增加能力：

| Entry-point group | 能力 |
|---|---|
| `astrabox.api.routers` | 在核心 API 路由之后、管理台 fallback 之前增加 FastAPI 路由 |
| `astrabox.web.middlewares` | 在 AstraBox 可信主机与身份检查内侧增加 Middleware |
| `astrabox.lifespan_hooks` | 随 AstraBox lifespan 启动和关闭扩展服务 |
| `astrabox.service_factories` | 替换一个受支持的服务实现 |
| `astrabox.providers.*` | 为 AstraBox Provider 接口增加实现 |

应用路由、Middleware 和 lifespan entry point 按名称顺序加载。名称重复、接口版本不兼容、
目标无效或启动报错都会阻止应用启动，不会静默跳过扩展。Lifespan hook 按进入顺序的逆序
退出。

通过 AstraBox entry point 安装的 Middleware 位于可信主机和身份检查内侧。必须包裹
整个应用的 Middleware 应安装到上层 ASGI 应用。

可用 Provider 接口见[系统架构](architecture.md#plugin-interfaces)。适配器指南分别介绍
[Agent 程序](writing-an-engine-adapter.md)和
[消息平台](writing-a-channel-provider.md)。

## 进程与安全要求

- 每个 Python 进程只运行一个 AstraBox 应用。Provider 注册表、当前选择、服务和后台
  任务由整个进程共用；
- 使用一个长期运行的事件循环。数据库客户端和后台任务属于进入 lifespan 的事件循环；
- 准确进入和退出一次 AstraBox lifespan。上层应用负责进程信号，AstraBox lifespan
  负责关闭自身服务；
- 正常配置 AstraBox 的认证与鉴权。共用进程不会让上层应用的用户自动获得 AstraBox
  资源访问权限；
- `ASTRABOX_ALLOWED_HOSTS`、TLS、回调地址、模型访问和沙箱网络必须与 ASGI 应用使用的
  域名一致；
- 上层应用需要其他事件循环策略时，请在启动 AstraBox 前配置 ASGI Server；否则
  `uvicorn[standard]` 可能选择 `uvloop`。

发布前，请使用生产环境的相同 ASGI 布局验证启动、登录、一次 Agent 任务及其流式输出、
已经配置的定时任务、关闭期间的就绪状态，以及最终进程退出。

## 相关文档

- [HTTP API](api.md)
- [部署 AstraBox](deploy.md)
- [系统架构](architecture.md)
