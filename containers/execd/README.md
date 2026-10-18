# Execd transport

This image carries OpenSandbox execd v1.1.0, source commit
`48b0215f1bd097b31d0f022a44640e00c11ac49d`, with `stream-upload.patch` and
`pty-output-drain.patch` applied.
The source archive is checksum-pinned. The Apache-2.0 licence is retained in the
image. Bootstrap, supervisor and isolation helpers come from the matching
official image; both ordinary and eBPF Linux execd binaries carry both patches.

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
