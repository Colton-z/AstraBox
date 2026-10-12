"""The data-plane faces a sandbox handle carries, independent of any backend.

Core reads and writes a box's files only through :class:`SandboxFiles`, and
runs commands in it only through :class:`SandboxCommands`: the ``files`` and
``commands`` properties of :class:`astrabox.seams.sandbox.SandboxHandle`. A
provider implements them natively, or reuses the standard implementations over
the execd daemon every built-in image carries
(:class:`astrabox.providers.execd.ExecdFiles` and
:class:`astrabox.providers.execd.ExecdCommands`). Either way,
:mod:`astrabox.testing.sandbox_data_conformance` is the suite that shows a live
box keeps this contract.

Paths are absolute and taken literally. Modes are POSIX permission bits
(``0o640``), never a provider's wire format, and owners and groups are account
names. Three file failures are typed: :class:`SandboxPathNotFound` when nothing
is at a path, :class:`SandboxPathRejected` when the box refuses the operation,
and :class:`astrabox.seams.sandbox.SandboxUnreachable` when the box cannot be
reached, which proves nothing about whether it still exists. A command's
non-zero exit status is data in its :class:`SandboxCommandResult`; a command
the box will not start raises :class:`SandboxCommandRefused`, and one it gives
no exit status for raises :class:`SandboxCommandOutcomeUnknown`.

A process that outlives any one connection to it, such as the terminal's shell
or an engine that speaks over its stdin and stdout, runs through
:class:`SandboxProcesses`, the optional ``processes`` face of a handle whose
provider declares ``supports_process_sessions``. The standard implementation is
:class:`astrabox.providers.execd.ExecdProcesses`.

A service a box runs on a port is reached at the :class:`SandboxEndpoint` its
handle resolves: an absolute URL and the headers every request to it carries.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Literal, Protocol, TypeAlias, runtime_checkable
from urllib.parse import urlsplit

#: What an entry is, as ``lstat`` sees it: a symlink is reported as itself and
#: never followed.
SandboxEntryKind = Literal["file", "directory", "symlink", "other"]


class SandboxPathNotFound(FileNotFoundError):
    """Nothing exists at the path, or at one of its parent components."""


class SandboxPathRejected(PermissionError):
    """The box refused the operation on this path; the message is its reason.

    Raised for a refusal such as a missing permission, a destination that
    already exists, or a path the implementation cannot address literally.
    """


@dataclass(frozen=True, slots=True)
class SandboxEntry:
    """One filesystem entry, as ``lstat`` reports it."""

    #: Absolute path in the box.
    path: str
    kind: SandboxEntryKind
    #: Size in bytes, as ``lstat`` reports it.
    size: int
    #: POSIX permission bits, for example ``0o640``.
    mode: int
    owner: str
    group: str
    modified_at: datetime | None


@dataclass(frozen=True, slots=True)
class SandboxDirectorySpec:
    """A directory to create, with the mode and ownership it takes when created."""

    path: str
    mode: int = 0o755
    owner: str | None = None
    group: str | None = None


@runtime_checkable
class SandboxFiles(Protocol):
    """A box's filesystem, addressed by absolute paths taken literally.

    A path must be absolute: a relative path raises ``ValueError``. No ``~``
    or ``$VAR`` in a path is ever expanded; an implementation that cannot
    address a path literally refuses it with :class:`SandboxPathRejected`.
    Each method raises :class:`SandboxPathNotFound` for a missing path,
    :class:`SandboxPathRejected` for a refusal by the box, and
    :class:`astrabox.seams.sandbox.SandboxUnreachable` when the box cannot be
    reached.
    """

    async def read_bytes(self, path: str) -> bytes:
        """Return the whole file.

        For the small files the platform writes and reads back. A caller that
        has to bound what it holds uses :meth:`read_chunks`.
        """
        ...

    def read_chunks(self, path: str) -> AsyncGenerator[bytes, None]:
        """Yield the file's bytes as they stream from the box.

        A failure surfaces when iteration starts. The caller caps what it
        keeps, and closes the generator (``aclose``) when it stops before the
        end, which releases the stream.
        """
        ...

    async def write_bytes(
        self,
        path: str,
        data: bytes | IO[bytes],
        *,
        mode: int = 0o644,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        """Create or replace the file with ``data``, given whole or as a binary stream.

        ``mode`` and the ownership take effect in the same call, so the file
        never stays behind with other permissions. Missing parent directories
        are created and take the same ownership.
        """
        ...

    async def make_directories(self, specs: Sequence[SandboxDirectorySpec]) -> None:
        """Create each directory and any missing parents, like ``mkdir -p``.

        Every directory the call creates takes the spec's owner and group, and
        the named directory takes its mode. A directory that already exists is
        left as it is.
        """
        ...

    async def stat(self, paths: Sequence[str]) -> Mapping[str, SandboxEntry]:
        """Return each path's entry, keyed by the path as given; never follows a symlink."""
        ...

    async def list_directory(self, path: str) -> Sequence[SandboxEntry]:
        """Return the directory's direct entries, not the directory itself."""
        ...

    async def search(self, root: str, pattern: str) -> Sequence[str]:
        """Return the paths below ``root`` whose name matches the glob ``pattern``.

        The match is on the entry's name, the last path component, and
        directories are not returned.
        """
        ...

    async def move(self, source: str, destination: str) -> None:
        """Rename ``source`` to ``destination``, creating its missing parents.

        An existing ``destination`` is refused with :class:`SandboxPathRejected`
        rather than replaced.
        """
        ...

    async def remove(self, paths: Sequence[str]) -> None:
        """Remove each path and everything below it, like ``rm -rf``.

        A symlink is removed itself and never followed, so the files it points
        to stay. A path that does not exist counts as removed.
        """
        ...


class SandboxCommandRefused(RuntimeError):
    """The box refused to start the command, so it did not run.

    The message is the box's reason, for example a rejected request or an
    access check that failed.
    """


class SandboxCommandOutcomeUnknown(RuntimeError):
    """The box gave no exit status for a command, so it is never read as a success.

    Raised when the box reported a failure that is not an exit status, or ended
    its report without one. What the command did, if it ran at all, is not
    known.
    """


@dataclass(frozen=True, slots=True)
class SandboxCommandResult:
    """How one command ended and what it printed.

    ``exit_code`` is ``0`` for a success and the process's own status for a
    non-zero exit. A negative value means a signal ended the process; which
    negative value is the backend's. A non-zero status is data, not an error.

    ``stdout`` and ``stderr`` are text reassembled from the command's output
    lines. Interior line breaks, blank lines included, are kept; a trailing
    newline, a carriage return before a newline, and bytes that are not UTF-8
    are not. A caller that needs exact bytes has the command write them to a
    file and reads it through :class:`SandboxFiles`.
    """

    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        """Everything the command printed: stdout, then stderr on the lines after it."""
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


@runtime_checkable
class SandboxCommands(Protocol):
    """Run a command in a box and report how it ended.

    ``command`` is one command line that the box runs with a POSIX shell, as in
    ``sh -c``, as root: the platform creates accounts and changes ownership
    through it. An implementation never switches account on a caller's behalf;
    a command that must run as another account says so itself, for example
    with ``runuser``. The command reads an empty stdin, and ``env`` is added to
    the environment it inherits.
    """

    async def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = 60.0,
    ) -> SandboxCommandResult:
        """Run ``command`` to its end and return how it ended.

        ``timeout`` is a wall-clock deadline in seconds, kept by the caller's
        side: past it, this raises ``TimeoutError``, and the implementation has
        the box stop the command shortly after. ``None`` waits for as long as
        the command runs.

        A non-zero exit status is returned, not raised. This raises
        :class:`SandboxCommandRefused` when the box will not start the command,
        :class:`SandboxCommandOutcomeUnknown` when it gives no exit status, and
        :class:`astrabox.seams.sandbox.SandboxUnreachable` when the box cannot
        be reached.
        """
        ...


class SandboxProcessDetached(RuntimeError):
    """The process, or this attachment to it, is gone; nothing more will arrive.

    Raised for a process id the box does not know, which includes every id
    once the box's process service has restarted, and by an attachment whose
    connection ended without the process's exit: another attachment took the
    process over, or the transport failed.
    """


class SandboxProcessAlreadyAttached(RuntimeError):
    """Another attachment holds the process, and this one asked not to take it over."""


class SandboxProcessReplayGap(RuntimeError):
    """The box has dropped the output an attachment asked to replay from.

    Resuming past the gap would drop output without a trace, so the attachment
    is refused. ``available`` is the earliest offset the box still holds.
    """

    def __init__(self, process_id: str, *, requested: int, available: int) -> None:
        super().__init__(
            f"process {process_id!r} output replay was truncated: "
            f"requested={requested} available={available}"
        )
        self.process_id = process_id
        self.requested = requested
        self.available = available


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    """Bytes the process wrote to one of its streams while this attachment held it."""

    stream: Literal["stdout", "stderr"]
    data: bytes


@dataclass(frozen=True, slots=True)
class ProcessReplay:
    """Output written before this attachment, replayed from the box.

    ``offset`` is where ``data`` starts in the process's output. The box keeps
    stdout and stderr as one stream for replay, so replayed bytes name neither.
    """

    offset: int
    data: bytes


@dataclass(frozen=True, slots=True)
class ProcessExit:
    """The process ended; ``exit_code`` is ``None`` when the box did not say how."""

    exit_code: int | None


#: One frame an attachment yields.
ProcessFrame: TypeAlias = ProcessOutput | ProcessReplay | ProcessExit


@runtime_checkable
class SandboxProcessAttachment(Protocol):
    """One connection holding a process: its output arriving, its stdin open."""

    def frames(self) -> AsyncGenerator[ProcessFrame, None]:
        """Yield the process's output as it arrives, for one consumer.

        A :class:`ProcessReplay` of what was asked to be replayed comes first.
        Everything the process wrote comes before its :class:`ProcessExit`,
        which is the last frame. Iteration ends quietly after the caller's own
        :meth:`detach`, and raises :class:`SandboxProcessDetached` when the
        connection ends any other way without the exit. A consumer that stops
        early closes the generator (``aclose``).
        """
        ...

    async def send(self, data: bytes) -> None:
        """Write ``data`` to the process's stdin."""
        ...

    async def detach(self) -> None:
        """Close this attachment and leave the process running."""
        ...


@runtime_checkable
class SandboxProcesses(Protocol):
    """Long-lived processes in a box, reachable by id from any platform replica.

    A process is named by the id :meth:`start` returns and outlives every
    connection to it. At most one attachment holds it at a time. Its output is
    numbered in bytes from its start, stdout and stderr together, and an
    attachment can have the box replay what it still holds from an offset.
    Every method raises :class:`astrabox.seams.sandbox.SandboxUnreachable`
    when the box cannot be reached, and ``RuntimeError`` carrying the box's
    answer when the box refuses.
    """

    async def start(self, command: str, *, cwd: str | None) -> str:
        """Register ``command`` to run in the directory ``cwd``; return its id.

        ``command`` is one shell command line run as root, reading its stdin
        from the attachment that holds it; ``cwd`` ``None`` leaves the box's
        default working directory. The process is running by the time the
        first :meth:`attach` returns, and an attachment that arrives after it
        exited runs it again.
        """
        ...

    async def exists(self, process_id: str) -> bool:
        """Whether the box knows the process, which may have exited."""
        ...

    async def output_offset(self, process_id: str) -> int:
        """Where the process's output ends now, in bytes from its start.

        Raises :class:`SandboxProcessDetached` for a process the box does not know.
        """
        ...

    async def attach(
        self, process_id: str, *, since: int, takeover: bool
    ) -> SandboxProcessAttachment:
        """Hold the process, with its output replayed from offset ``since``.

        ``takeover=True`` ends the attachment holding the process, if there is
        one; ``False`` raises :class:`SandboxProcessAlreadyAttached` instead.
        Raises :class:`SandboxProcessReplayGap` when the box has dropped the
        output at ``since``, and :class:`SandboxProcessDetached` for a
        process the box does not know.
        """
        ...

    async def signal(self, process_id: str, name: str) -> None:
        """Send the signal ``name``, such as ``"SIGINT"``, to the process and its children.

        A process that is not running has nothing to signal, and this returns.
        Signalling may end the attachment that holds the process. Raises
        :class:`SandboxProcessDetached` for a process the box does not know.
        """
        ...

    async def close(self, process_id: str) -> None:
        """Stop the process and forget it; an id the box does not know is already closed."""
        ...


@dataclass(frozen=True, slots=True)
class SandboxEndpoint:
    """Where the host reaches one port in a box.

    ``url`` is an absolute ``http`` or ``https`` URL without a trailing slash;
    a WebSocket client uses the same address with the ``ws`` or ``wss``
    scheme. ``headers`` go on every request to it: a deployment may route or
    admit traffic by them, so a request without them can work against an open
    server and fail behind a hardened ingress. ``expires_at`` is set only on a
    signed endpoint, whose URL itself carries a credential that expires then.
    """

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        address = urlsplit(str(self.url))
        if address.scheme not in {"http", "https"} or not address.netloc:
            raise ValueError(f"a sandbox endpoint is an absolute http(s) URL, not {self.url!r}")
        object.__setattr__(self, "url", str(self.url).rstrip("/"))
        object.__setattr__(self, "headers", dict(self.headers))


__all__ = [
    "ProcessExit",
    "ProcessFrame",
    "ProcessOutput",
    "ProcessReplay",
    "SandboxCommandOutcomeUnknown",
    "SandboxCommandRefused",
    "SandboxCommandResult",
    "SandboxCommands",
    "SandboxDirectorySpec",
    "SandboxEndpoint",
    "SandboxEntry",
    "SandboxEntryKind",
    "SandboxFiles",
    "SandboxPathNotFound",
    "SandboxPathRejected",
    "SandboxProcessAlreadyAttached",
    "SandboxProcessAttachment",
    "SandboxProcessDetached",
    "SandboxProcessReplayGap",
    "SandboxProcesses",
]
