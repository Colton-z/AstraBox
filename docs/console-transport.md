# Extend the console's HTTP transport

A deployment that supplies browser authentication can install one transport
before the console starts. Set `globalThis.astraboxConsoleTransport` to an
function `(request: Request) => Promise<Response>`. The TypeScript type is exported
as `ConsoleTransport` from `frontend/src/consoleTransport.ts`.

For a custom console build, use a deployment entry module as the HTML entry:

```ts
import { fetchWithAuthRecovery } from './deployment/authRecovery';
import type { ConsoleTransport } from './consoleTransport';

globalThis.astraboxConsoleTransport = (
  (request) => fetchWithAuthRecovery(request)
) satisfies ConsoleTransport;

await import('./main');
```

The deployment supplies `authRecovery`; it is not an AstraBox module. Bundle
this entry with the console. The dynamic import starts the console only after
registration. A deployment serving prebuilt assets can instead load its own
bootstrap script before the console entry executes. Do not replace
`globalThis.fetch`, install a Service Worker, or copy the API client. Keep the
transport installed for the lifetime of the document.

Without registration, console API requests use the browser's native fetch.
With registration, the transport receives an absolute URL and the original
method, headers, credentials, body, cancellation signal and other fetch
options as a standard `Request`. FormData is framed by the browser, including
its matching multipart boundary. Return the actual `Response`; the console
then applies its existing envelope parsing and error presentation. A configured
transport failure propagates; AstraBox does not bypass it with another fetch.

## Coverage and composition

The transport covers generated API calls, identity probes and logout, uploads,
file-content reads, terminal streams, Session follow streams and Artifact API
requests, including live streams and presence cleanup. It does not intercept
browser navigation, download links, iframe documents, WebSockets, blob URLs,
the console's own release-document check, or sandbox preview URLs.

For operations that already use network recovery, the order is:

1. The API prepares its request and starts any caller-owned deadline.
2. Existing network recovery invokes the configured transport on each attempt.
3. The deployment transport calls native fetch, inspects its own authentication
   protocol, and returns the final response.
4. The API parses that response and presents success or failure.

Network recovery only handles thrown transport failures; an HTTP response,
including 500, does not cause it to retry. Reads retain their existing short
network retry budget. Only mutations whose callers explicitly declare a
server-side replay guarantee have network retries. Interaction answers and
ordinary POSTs have no such retry. Streaming paths retain their existing
cursor and liveness handling without an additional network-recovery loop here.

## Authentication and replay belong to the deployment

The deployment must classify its own authentication protocol. AstraBox cannot
infer that a controller did not execute from an HTTP 500, a message string or
a successful HTTP status. A transport may replay a mutation only when the
deployment's authentication boundary proves rejection before business execution.
An uncertain delivery, business failure, gateway HTML response or permission
refusal must not become an authentication retry.

Before sending a replayable request, retain `request.clone()`; fetch consumes
request bodies. Inspect a response through `response.clone()` when needed,
leaving the returned response readable. Do not buffer a successful event stream
to inspect authentication. Cloning streamed bodies can buffer unread bytes, so
the deployment must bound its recovery and body handling. These are native
[Request.clone() semantics](https://developer.mozilla.org/en-US/docs/Web/API/Request/clone),
also used by the request-based custom-fetch contract in
[openapi-fetch](https://openapi-ts.dev/openapi-fetch/api#createclient).

Coalesce authentication probes within the deployment transport and use native
fetch for those probes, avoiding recursion through the console transport.
Retain `request.signal` on every attempt and stop recovery waits and replays
when it aborts. Per-attempt timeouts must also respect that caller signal;
registration does not reset the interaction answer's existing 15-second
deadline. The transport must not refresh the document or discard form state
as part of transient recovery.

Exclude logout from automatic authentication recovery: signing out must not
renew the login or replay the logout POST. Preserve its redirect mode and
[confirmation/navigation contract](team-login.md#sign-out). A returned provider
logout URL still requires top-level navigation by the account menu.

This entry point provides transport integration. The deployment still owns its
authentication classifier, probe endpoint, deduplication, recovery budget and
evidence that any replay is safe.
