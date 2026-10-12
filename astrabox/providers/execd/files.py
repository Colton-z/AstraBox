"""The files face over execd, the in-box daemon every built-in image carries.

execd (``opensandbox/execd``, the release the images and the lifecycle server
pin) serves a filesystem API on the box's execd port. :class:`ExecdFiles`
implements :class:`astrabox.seams.sandbox_data.SandboxFiles` on that API
through the OpenSandbox SDK's execd client, and keeps execd's wire rules out of
core:

* a mode travels as its octal digits read as a decimal number, so ``0o640``
  is sent as ``640`` and read back the same way;
* execd expands ``$VAR`` in a path from its own environment before using it,
  so a path holding ``$`` cannot be addressed literally and is refused;
* execd reads a requested mode of zero as "leave the default", so a zero mode
  is refused rather than silently replaced;
* a missing path answers 404 and any other refusal another HTTP error status,
  which become :class:`SandboxPathNotFound` and :class:`SandboxPathRejected`;
* :meth:`ExecdFiles.remove` uses execd's recursive removal, which unlinks a
  symlink instead of following it;
* execd answers an upload only after it has copied the received body from its
  multipart spool to the target and synced the target to disk
  (``components/execd/pkg/web/controller/filesystem_upload.go`` in OpenSandbox),
  so the wait for that answer grows with the file, as described at
  :data:`_UPLOAD_SETTLE_FLOOR_BYTES_PER_SECOND`.
"""

from __future__ import annotations

import io
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from datetime import timedelta
from typing import IO
from urllib.parse import urlsplit

import httpx
from opensandbox.adapters.factory import AdapterFactory
from opensandbox.config import ConnectionConfig
from opensandbox.constants import DEFAULT_EXECD_PORT
from opensandbox.exceptions import (
    SandboxApiException,
    SandboxConnectionException,
    SandboxTimeoutException,
)
from opensandbox.models.filesystem import (
    DirectoryListEntry,
    EntryInfo,
    MoveEntry,
    SearchEntry,
    WriteEntry,
)
from opensandbox.models.sandboxes import SandboxEndpoint
from opensandbox.sandbox import Sandbox
from opensandbox.services.filesystem import Filesystem

from astrabox.seams.sandbox import SandboxUnreachable
from astrabox.seams.sandbox_data import (
    SandboxDirectorySpec,
    SandboxEntry,
    SandboxEntryKind,
    SandboxPathNotFound,
    SandboxPathRejected,
)

#: execd's entry types, which are exactly the contract's kinds.
_KINDS: dict[str, SandboxEntryKind] = {
    "file": "file",
    "directory": "directory",
    "symlink": "symlink",
    "other": "other",
}

#: The slowest rate, in bytes per second, at which execd is expected to settle
#: an upload. Settling means copying the received body from execd's multipart
#: spool to the target and syncing it to disk, so execd's answer can lag the
#: last request byte by the time it takes to write the whole file again. The
#: wait for that answer is therefore the client's ordinary request timeout
#: plus the upload's size at this rate. The rate is far below the sustained
#: write rate of the disks sandboxes run on, so a healthy box answers within
#: the wait, while a box lost after the body is still reported, in a time that
#: grows with the file.
_UPLOAD_SETTLE_FLOOR_BYTES_PER_SECOND = 8 * 1024 * 1024


def _upload_settle_time(content: bytes | io.IOBase) -> timedelta:
    """How much longer than an ordinary request execd may take to answer this upload.

    Whole seconds, rounded down: an upload small enough to settle within a
    second needs nothing beyond the ordinary request timeout, and keeps the
    ordinary client. The SDK sends a seekable stream from its start, so that
    is where the size is measured from. A stream whose size cannot be
    measured gets no extra time.
    """

    if isinstance(content, bytes):
        size = len(content)
    elif content.seekable():
        size = content.seek(0, io.SEEK_END)
        content.seek(0)
    else:
        return timedelta(0)
    return timedelta(seconds=size // _UPLOAD_SETTLE_FLOOR_BYTES_PER_SECOND)


def _wire_mode(mode: int) -> int:
    """The mode as execd reads it: the octal digits taken as a decimal number."""

    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 < mode <= 0o777:
        raise ValueError(
            f"a mode is permission bits from 0o001 to 0o777, not {mode!r}; "
            "execd reads zero as 'leave the default'"
        )
    return int(format(mode, "o"))


def _posix_mode(wire: int) -> int:
    try:
        return int(str(int(wire)), 8)
    except ValueError as exc:
        raise RuntimeError(f"execd reported mode {wire!r}, which is not octal digits") from exc


class ExecdFiles:
    """:class:`astrabox.seams.sandbox_data.SandboxFiles` over one execd.

    Build it with :meth:`of_sandbox` from an SDK sandbox the caller already
    holds and closes, or with :meth:`at_endpoint` from the URL and request
    headers a provider's ``get_endpoint`` resolves. The second opens a
    connection for each operation and closes it after, so the object holds
    nothing that needs closing.

    ``open_filesystem`` opens the SDK client for one operation. Its argument
    is how much longer than an ordinary request that operation may wait for
    execd's answer, which is zero except for an upload.
    """

    def __init__(
        self,
        open_filesystem: Callable[[timedelta], AbstractAsyncContextManager[Filesystem]],
        *,
        sandbox_id: str,
    ) -> None:
        self._open = open_filesystem
        self._sandbox_id = str(sandbox_id)

    @classmethod
    def of_sandbox(cls, sandbox: Sandbox) -> ExecdFiles:
        """Use the execd client an SDK sandbox holds, which closing the sandbox closes.

        An upload large enough to outlast that client's request timeout goes
        through a client of its own, whose timeout is extended by the upload's
        :func:`_upload_settle_time`. That client shares the sandbox's
        transport, so closing the sandbox closes its connection too.
        """

        @asynccontextmanager
        async def client(settle: timedelta) -> AsyncIterator[Filesystem]:
            if not settle:
                yield sandbox.files
                return
            config = sandbox.connection_config
            # The copy keeps the sandbox's transport and its claim to own it,
            # so it must never be closed itself: the sandbox closes that
            # transport.
            extended = config.model_copy(
                update={"request_timeout": config.request_timeout + settle}
            )
            endpoint = await sandbox.get_endpoint(DEFAULT_EXECD_PORT)
            yield AdapterFactory(extended).create_filesystem_service(endpoint)

        return cls(client, sandbox_id=str(sandbox.id))

    @classmethod
    def at_endpoint(
        cls, url: str, headers: Mapping[str, str], *, sandbox_id: str
    ) -> ExecdFiles:
        """Reach the execd at ``url``, sending ``headers`` on every request."""

        address = urlsplit(url)
        if address.scheme not in {"http", "https"} or not address.netloc:
            raise ValueError(f"an execd endpoint needs an http(s) URL, not {url!r}")
        endpoint = SandboxEndpoint(
            endpoint=address.netloc + address.path.rstrip("/"), headers=dict(headers)
        )

        @asynccontextmanager
        async def connection(settle: timedelta) -> AsyncIterator[Filesystem]:
            ordinary = ConnectionConfig(protocol=address.scheme)
            config = ordinary.model_copy(
                update={"request_timeout": ordinary.request_timeout + settle}
            ).with_transport_if_missing()
            try:
                yield AdapterFactory(config).create_filesystem_service(endpoint)
            finally:
                await config.close_transport_if_owned()

        return cls(connection, sandbox_id=sandbox_id)

    # ── the contract ─────────────────────────────────────────────────────

    async def read_bytes(self, path: str) -> bytes:
        target = self._literal(path)
        async with self._open(timedelta(0)) as filesystem:
            with self._translated("read", target):
                return await filesystem.read_bytes(target)

    async def read_chunks(self, path: str) -> AsyncGenerator[bytes, None]:
        target = self._literal(path)
        async with self._open(timedelta(0)) as filesystem:
            with self._translated("read", target):
                stream = await filesystem.read_bytes_stream(target)
                try:
                    async for chunk in stream:
                        yield bytes(chunk)
                finally:
                    # The SDK hands back the response's own byte generator, and
                    # closing it stops the read. httpx returns the connection
                    # only once a body is read to its end, so a read stopped
                    # early holds its connection until the client's transport
                    # closes: with the sandbox for :meth:`of_sandbox`, and when
                    # this operation ends for :meth:`at_endpoint`.
                    if isinstance(stream, AsyncGenerator):
                        await stream.aclose()

    async def write_bytes(
        self,
        path: str,
        data: bytes | IO[bytes],
        *,
        mode: int = 0o644,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        target = self._literal(path)
        if isinstance(data, (bytes, bytearray, memoryview)):
            content: bytes | io.IOBase = bytes(data)
        elif isinstance(data, io.IOBase):
            content = data
        else:
            raise TypeError(f"file content is bytes or a binary stream, not {type(data).__name__}")
        entry = WriteEntry(
            path=target, data=content, mode=_wire_mode(mode), owner=owner, group=group
        )
        with self._translated("write", target):
            async with self._open(_upload_settle_time(content)) as filesystem:
                await filesystem.write_files([entry])

    async def make_directories(self, specs: Sequence[SandboxDirectorySpec]) -> None:
        entries = [
            WriteEntry(
                path=self._literal(spec.path),
                mode=_wire_mode(spec.mode),
                owner=spec.owner,
                group=spec.group,
            )
            for spec in specs
        ]
        if not entries:
            return
        async with self._open(timedelta(0)) as filesystem:
            with self._translated("create", ", ".join(entry.path for entry in entries)):
                await filesystem.create_directories(entries)

    async def stat(self, paths: Sequence[str]) -> Mapping[str, SandboxEntry]:
        targets = [self._literal(path) for path in paths]
        if not targets:
            return {}
        async with self._open(timedelta(0)) as filesystem:
            with self._translated("stat", ", ".join(targets)):
                infos = await filesystem.get_file_info(targets)
        entries: dict[str, SandboxEntry] = {}
        for target in targets:
            info = infos.get(target)
            if info is None:
                raise RuntimeError(f"execd answered a stat without {target!r}")
            entries[target] = self._entry(info)
        return entries

    async def list_directory(self, path: str) -> Sequence[SandboxEntry]:
        target = self._literal(path)
        async with self._open(timedelta(0)) as filesystem:
            with self._translated("list", target):
                infos = await filesystem.list_directory(DirectoryListEntry(path=target, depth=1))
        return [self._entry(info) for info in infos]

    async def search(self, root: str, pattern: str) -> Sequence[str]:
        target = self._literal(root)
        async with self._open(timedelta(0)) as filesystem:
            with self._translated("search", target):
                infos = await filesystem.search(SearchEntry(path=target, pattern=pattern))
        return [info.path for info in infos]

    async def move(self, source: str, destination: str) -> None:
        origin = self._literal(source)
        target = self._literal(destination)
        async with self._open(timedelta(0)) as filesystem:
            with self._translated("move", f"{origin} to {target}"):
                await filesystem.move_files([MoveEntry(source=origin, destination=target)])

    async def remove(self, paths: Sequence[str]) -> None:
        targets = [self._literal(path) for path in paths]
        if not targets:
            return
        async with self._open(timedelta(0)) as filesystem:
            with self._translated("remove", ", ".join(targets)):
                await filesystem.delete_directories(targets)

    # ── translation ──────────────────────────────────────────────────────

    @staticmethod
    def _literal(path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"a sandbox path must be absolute, not {path!r}")
        if "$" in path:
            raise SandboxPathRejected(
                f"execd expands '$' in a path, so {path!r} cannot be addressed literally"
            )
        return path

    @contextmanager
    def _translated(self, operation: str, subject: str) -> Iterator[None]:
        try:
            yield
        except SandboxApiException as exc:
            status = exc.status_code
            if status == 404:
                raise SandboxPathNotFound(f"{subject}: {exc}") from exc
            if isinstance(status, int) and status >= 400:
                raise SandboxPathRejected(f"execd refused to {operation} {subject}: {exc}") from exc
            raise
        except (SandboxConnectionException, SandboxTimeoutException, httpx.TransportError) as exc:
            raise SandboxUnreachable(
                self._sandbox_id, f"execd could not {operation} {subject}: {exc}"
            ) from exc

    @staticmethod
    def _entry(info: EntryInfo) -> SandboxEntry:
        kind = _KINDS.get(str(info.entry_type or ""))
        if kind is None:
            raise RuntimeError(
                f"execd reported {info.path!r} as {info.entry_type!r}, which is not an entry kind"
            )
        return SandboxEntry(
            path=info.path,
            kind=kind,
            size=int(info.size),
            mode=_posix_mode(info.mode),
            owner=info.owner,
            group=info.group,
            modified_at=info.modified_at,
        )


__all__ = ["ExecdFiles"]
