# Execd transport

This image carries OpenSandbox execd from release 1.2.1, source commit
`72e9feda4356a3087ae279af254ac67bb98c0129` (the commit the release's images
are built from, per `docs/releases/1.2.1.yaml`), with `stream-upload.patch`,
`pty-refusal-frames.patch` and `access-token.patch` applied. The source archive is checksum-pinned.
The Apache-2.0 licence is retained in the image. Bootstrap, supervisor and
isolation helpers come from the matching official image
`opensandbox/execd:release-1.2.1`, pinned by digest; both ordinary and eBPF
Linux execd binaries carry these patches.

The supplier's `specs/execd-api.yaml` defines ordered metadata/file pairs.
The supplier's handler parses the whole multipart body first, which spools
every file part to memory or temporary files before writing any of them. The
patched handler uses Go's `Request.MultipartReader` to pass each file part to
the supplier's writer as it arrives. That writer stages the bytes beside the
target, syncs them, renames them into place and syncs the directory; its
error handling and ownership/mode operations are unchanged. Metadata decoding
is unchanged. No file-sized buffer or buffered alternate path is introduced.

A failed write leaves the file it was replacing intact, except where the
target's directory is not writable: the supplier then writes in place, and a
failed write can leave a partial file. Within a batch, files before the
failing one have already been written.

Pipe-mode process holders send the exit frame only after the output the
process wrote has reached the WebSocket: the supplier's holder drainer waits
for the process to exit, then up to two seconds for its output broadcasters
to finish, then for the WebSocket pumps (`startPTYHolderOutputDrainer` in
`components/execd/pkg/web/controller/pty_ws.go`).

`ExecdProcesses` requests `errors=frame` on holder WebSocket attachments. The
patched daemon upgrades a refused attachment and sends its existing error frame
with `ALREADY_CONNECTED` or `SESSION_GONE`, then closes with code 1008. This
preserves an explicit refusal through proxies that upgrade before connecting
to the daemon. Clients without this query keep HTTP 409/404 refusals. The
existing holder lock and post-upgrade takeover ordering are unchanged; refusing
another attachment does not evict the holder. The SDK maps these machine codes
to its existing exceptions and refuses unexpected initial control frames as
protocol errors. It does not infer occupancy from a proxy message or disconnect.

The supplier's execd admits callers by its access token (`EXECD_ACCESS_TOKEN`,
sent as `X-EXECD-ACCESS-TOKEN`) on every route except `/ping`, `/ready` and
`POST /internal/init`. That last route replaces the token: given
`preserveRuntimeState` and a caller-chosen `accessTokenHash`, execd then admits
the caller's token and refuses the configured one, so any caller that reaches
the port can take the token over. With `access-token.patch`,
`/internal/init` is open only while no token is in force (neither the
environment token nor an applied token hash); afterwards it requires the token
like every other route. execd authenticates before it validates a request, so
an empty `POST /internal/init` without the token answers 401 from this build
and 400 from the supplier's; AstraBox uses that answer to refuse an execd image
without the patch (`astrabox/providers/open_sandbox/execd_access.py`). The patch
also removes `X-EXECD-ACCESS-TOKEN` from requests that `/proxy/<port>` passes
to another in-box service, as the supplier's per-user filesystem proxy
(`filesystem_identity_linux.go`) already does, so a service running as the
workload's account never receives execd's credential. A request for execd's own
port keeps it: on the Docker runtime the lifecycle server's endpoint for execd
is `/proxy/44772` on execd itself, so every SDK request to execd passes through
that route.

The Claude Code, Codex, DeepSeek Harness and Pi image targets build the default
execd image first. An explicit `EXECD_IMAGE` selects an existing image without
rebuilding it; sandbox targets pass that image through `SANDBOX_EXECD_IMAGE`.
To build execd at a custom tag, run `make build-execd-image EXECD_IMAGE=<tag>`
explicitly. The release builder builds or restores execd before its consumers,
and the component planner includes its inputs in their fingerprints.

The lifecycle server uses the release's execd image for new boxes. Set
`ASTRABOX_SANDBOX_SERVER_EXECD_IMAGE` to select another compatible init image.
Release deployment records and selects its digest. Runtime Dockerfiles copy
the same artifact for isolation sessions; Hermes uses lifecycle injection.
Existing boxes retain the binary with which they were created.
