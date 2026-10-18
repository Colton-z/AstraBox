# Execd transport

This image carries OpenSandbox execd v1.1.0, source commit
`48b0215f1bd097b31d0f022a44640e00c11ac49d`, with `stream-upload.patch` and
`pty-output-drain.patch` and `pty-refusal-frames.patch` applied.
The source archive is checksum-pinned. The Apache-2.0 licence is retained in the
image. Bootstrap, supervisor and isolation helpers come from the matching
official image; both ordinary and eBPF Linux execd binaries carry these patches.

The supplier's `specs/execd-api.yaml` defines ordered metadata/file pairs.
The patched handler uses Go's `Request.MultipartReader` to stream each file
part through the existing writer, without whole-body multipart spooling. The
writer retains `io.Copy`, file sync and close, parent-directory sync, native
error handling and ownership/mode operations. Metadata decoding is unchanged.
No file-sized buffer or buffered alternate path is introduced.

Uploads remain non-atomic: a failed write can leave a partial destination, and
an earlier file in a batch can already have completed.

Pipe-mode process holders wait for the output broadcasters and WebSocket pumps
to finish before sending the exit frame and closing the connection. Detaching
or taking over cancels that wait and joins the drainer before releasing the
holder lock. This uses the supplier's output-completion signal; it does not
depend on a client delay or replay to recover missing live output. Providers
reusing `ExecdProcesses` need this patched binary for complete holder output;
the unmodified `opensandbox/execd:v1.1.0` holder can close before output drains.

`ExecdProcesses` requests `errors=frame` on holder WebSocket attachments. The
patched daemon upgrades a refused attachment and sends its existing error frame
with `ALREADY_CONNECTED` or `SESSION_GONE`, then closes with code 1008. This
preserves an explicit refusal through proxies that upgrade before connecting
to the daemon. Clients without this query keep HTTP 409/404 refusals. The
existing holder lock and post-upgrade takeover ordering are unchanged; refusing
another attachment does not evict the holder. The SDK maps these machine codes
to its existing exceptions and refuses unexpected initial control frames as
protocol errors. It does not infer occupancy from a proxy message or disconnect.

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
