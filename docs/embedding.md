# Use AstraBox from a Python application

Most applications run AstraBox as a separate service and use its HTTP API. A
Python application that must share the same process can instead load AstraBox
as an ASGI application with `create_app()`. Specialized Python workers can use
the service layer directly when they intentionally own every lifecycle step
they need.

Both options use the same Agents, Sessions, streaming responses, Deployments,
credentials, and sandbox lifecycle. In-process use does not move the Agent
program into the host application: the Agent program still runs in an
OpenSandbox sandbox and remains available after the developer's computer
disconnects.

## Integration options

| | HTTP service | ASGI application in the host process |
|---|---|---|
| Works with | Any language or framework | Python and an ASGI server |
| Deployment | AstraBox is deployed and scaled independently | AstraBox and the host application are deployed together |
| Failure and dependency isolation | Separate process and Python environment | Shared process, dependencies, event loop, and failures |
| Access | HTTP, SSE, WebSocket, and remote MCP | The same routes through the ASGI application |
| Extensions | Installed AstraBox extension packages | Installed extensions plus an outer ASGI wrapper when needed |
| Upgrade | Upgrade AstraBox independently | Test and upgrade the combined application |

Use the HTTP service when the application only needs to create or use Agents.
Use the ASGI application when one Python process or direct ASGI composition is
a deliberate requirement.

## Run AstraBox as an HTTP service

Start the application on loopback:

```bash
astrabox serve --host 127.0.0.1 --port 8088
```

The AstraBox process initializes storage, runs migrations, starts background
recovery and scheduled work, reports readiness, drains active work, and closes
connections during shutdown. The calling application only needs to authenticate
and use the published API.

See [HTTP API](api.md), [API authentication](api-authentication.md), and
[Event Stream](events-stream.md) for the application-facing interface. See
[Deploy AstraBox](deploy.md) when the service will be available outside one
trusted host.

## Load the ASGI application

`create_app()` returns the FastAPI application used by `astrabox serve`:

```python
from astrabox.api.app import create_app

astrabox_app = create_app()
```

Set AstraBox environment variables before calling `create_app()`. Creating the
object registers routes and middleware but does not connect to the database or
sandbox service. Those operations begin when the application lifespan starts.

An ASGI server can run the factory directly:

```bash
uvicorn astrabox.api.app:create_app --factory --host 127.0.0.1 --port 8088
```

This shares the launch mechanism, not the process, with another application.
For one combined process, mount AstraBox at the root of a parent FastAPI app and
enter the AstraBox lifespan from the parent's lifespan:

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


# Register host routes first. AstraBox handles every remaining root path.
app.mount("/", astrabox_app)
```

FastAPI runs lifespan events only for the main application, not automatically
for a mounted subapplication. Enter the AstraBox lifespan exactly once as shown
above; otherwise migrations, recovery, scheduled work, and shutdown do not run.

## Keep AstraBox at the origin root

The AstraBox console uses root-relative asset, API, authentication, and console
routes such as `/assets`, `/api`, and `/manage`. Mounting the application at a
path such as `/astrabox` is therefore not supported.

The parent application can register its own routes before the root mount. If
both applications need independent root routes or browser interfaces, give
AstraBox its own hostname or route a separate virtual host to the ASGI
application. Path-prefix hosting is not supported for the bundled console.

## What the lifespan starts

The AstraBox lifespan owns the complete service lifecycle:

- loads installed extensions and Provider implementations;
- prepares the database and runs pending migrations;
- starts Deployment scheduling and runtime recovery;
- enters installed lifespan hooks after core startup;
- changes `/readyz` to not ready before shutdown;
- drains active work and closes runtime connections.

Required extensions, database preparation, migrations, and Deployment scheduling
must initialize successfully. Runtime recovery startup errors are logged without
aborting boot; the first API request can retry that initialization. The host
must not accept requests until the lifespan has entered successfully.

## Call AstraBox services without HTTP

A Python worker that does not need the console or HTTP routes can initialize the
provider composition, storage, migrations, and runtime recovery explicitly:

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
    # Call the required platform service with an explicit user context.
    ...
finally:
    await run_lifecycle_shutdown(reason="host_shutdown")
```

`bootstrap()` is idempotent. It loads built-in and installed Providers, selects
the configured sandbox service, and rejects incompatible deployment settings.
It does not prepare storage, run migrations, start Deployment scheduling, enter
application lifespan hooks, or arrange shutdown.

The example starts the recovery lifecycle used by AstraBox services, but it is
not a substitute for the full application lifespan. Use `create_app()` when the
host needs scheduled Deployments, app extensions, readiness and draining, the
console, or any HTTP surface. Direct service calls must still pass an explicit
user identity so normal authorization remains in effect.

## Extend the application

An installed Python package can add behavior through entry points:

| Entry-point group | Capability |
|---|---|
| `astrabox.api.routers` | Add FastAPI routes after core API routes and before the console fallback |
| `astrabox.web.middlewares` | Add middleware inside AstraBox's trusted-host and identity checks |
| `astrabox.lifespan_hooks` | Start and stop extension services with the AstraBox lifespan |
| `astrabox.service_factories` | Replace a supported service implementation |
| `astrabox.providers.*` | Add implementations for an AstraBox Provider interface |

Application router, middleware, and lifespan entry points load in name order.
Duplicate names, incompatible interface versions, invalid targets, and startup
errors stop the application instead of silently skipping the extension.
Lifespan hooks exit in reverse order.

Middleware installed through an AstraBox entry point runs after the trusted-host
and identity checks. Middleware that must wrap the complete application belongs
on the parent ASGI app.

See [Architecture](architecture.md#plugin-interfaces) for the available Provider
interfaces. The adapter-specific guides cover
[Agent programs](writing-an-engine-adapter.md) and
[messaging platforms](writing-a-channel-provider.md).

## Process and security requirements

- Run one AstraBox application per Python process. Provider registries, selected
  defaults, services, and background workers are process-wide.
- Use one long-lived event loop. Database clients and background tasks are bound
  to the loop that enters the lifespan.
- Enter and exit the AstraBox lifespan once. The parent owns process signals;
  the AstraBox lifespan owns its service shutdown.
- Configure AstraBox authentication and authorization normally. Sharing a
  process does not grant the host application's users access to AstraBox
  resources.
- Keep `ASTRABOX_ALLOWED_HOSTS`, TLS, callback addresses, model access, and
  sandbox networking consistent with the hostname used for the embedded app.
- If the host requires a different event-loop policy, configure the ASGI server
  before it starts AstraBox. `uvicorn[standard]` may otherwise select `uvloop`.

Before release, exercise startup, login, one Agent task and its stream,
scheduled work when configured, readiness during shutdown, and final process
exit using the same ASGI layout used in production.

## Related documents

- [HTTP API](api.md)
- [Deploy AstraBox](deploy.md)
- [Architecture](architecture.md)
