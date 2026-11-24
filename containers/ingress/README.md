# Ingress gateway

This image carries the OpenSandbox ingress gateway from release 1.2.1, source
commit `72e9feda4356a3087ae279af254ac67bb98c0129` (the commit the release's
images are built from, per `docs/releases/1.2.1.yaml`), with
`full-duplex.patch` applied, on the official `opensandbox/ingress:release-1.2.1`
image pinned by digest. The source archive is checksum-pinned and the Apache-2.0
licence is retained in the image. A Kubernetes deployment in gateway mode
installs the official chart with this image
(`docs/providers/opensandbox.md`, "Configure a browser-facing Kubernetes
Gateway").

The gateway forwards each request with `httputil.ReverseProxy`. After copying a
request body of known length, the upstream `Transport` reads the body once more
to see its end, and a sandbox can answer before that read. Go's HTTP/1 server is
half duplex by default: its first response write discards and closes the request
body. The `Transport`'s read then fails and it closes the upstream connection,
cutting the response the sandbox is still streaming. execd sees that as a
client disconnect and stops the command; for an isolated-session run it
interrupts the session's process group, which ends the session.
Every POST with a body and a streamed answer is exposed, so isolated-session
runs and execd commands fail intermittently through the gateway while the same
requests to the Pod address do not.

The patch enables full duplex (`http.ResponseController.EnableFullDuplex`) for
every request before it is proxied, so the request body stays readable while the
response is written. HTTP/2 requests are always full duplex. Routing,
authentication, wake and WebSocket handling are unchanged.
