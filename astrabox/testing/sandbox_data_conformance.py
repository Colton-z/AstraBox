"""Live contract suite for a sandbox handle's files, commands and processes faces.

The suite drives one real box through
:class:`astrabox.seams.sandbox_data.SandboxFiles`,
:class:`astrabox.seams.sandbox_data.SandboxCommands` and
:class:`astrabox.seams.sandbox_data.SandboxProcesses` and checks what core
relies on. For files: exact bytes whole and streamed, the mode and ownership a
write or ``mkdir`` gives, ``lstat`` kinds including symlinks, the one-level
listing, name search, moves that refuse an existing destination, removal that
never follows a symlink, and paths taken literally. For commands: a shell
command line run as root, an exit status returned as data, stdout and stderr
apart, interior lines kept, the environment passed literally, an empty stdin, a
signal reported as a negative status, and a deadline that raises
``TimeoutError`` and stops the command in the box. For processes, checked when
the binding hands the suite a ``processes`` face because its provider declares
``supports_process_sessions``: stdin in and the exit last, a reattach that
replays from an offset, one holder unless another takes over, a replay that
never starts past its cursor, a signal that reaches only a running process,
``close`` that stops a process and forgets it, and an exited process that runs
again when attached. A fake cannot show any of this, so a binding needs a live
box of its own provider.

A downstream binds it in its own live tests, one case per check::

    from astrabox.testing.sandbox_data_conformance import (
        DATA_CONTRACT, PROCESSES_CONTRACT, DataContractBox, run_contract_check,
    )

    @pytest.mark.parametrize(
        "check", DATA_CONTRACT + PROCESSES_CONTRACT, ids=lambda check: check.name
    )
    async def test_data_contract(check, live_box) -> None:
        await run_contract_check(check, DataContractBox(
            files=live_box.files,
            commands=live_box.commands,
            scratch="/tmp/data-contract",
            account="agent",
            processes=live_box.processes,
        ))

leaving ``PROCESSES_CONTRACT`` out for a provider without process sessions,
or runs every check at once with :func:`check_data_contract`. The in-tree
binding, ``tests/e2e/sandbox_data_conformance_test.py``, runs it on a box of the
built-in OpenSandbox provider.
"""

from __future__ import annotations

import asyncio
import io
import posixpath
import shlex
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass

from astrabox.seams.sandbox_data import (
    ProcessExit,
    ProcessFrame,
    ProcessOutput,
    ProcessReplay,
    SandboxCommands,
    SandboxDirectorySpec,
    SandboxFiles,
    SandboxPathNotFound,
    SandboxPathRejected,
    SandboxProcessAlreadyAttached,
    SandboxProcessDetached,
    SandboxProcesses,
    SandboxProcessReplayGap,
)


@dataclass(frozen=True, slots=True)
class DataContractBox:
    """The live box a binding hands the suite."""

    files: SandboxFiles
    commands: SandboxCommands
    #: An absolute directory the suite creates, works in, and removes.
    scratch: str
    #: An account, with a group of the same name, that the box knows and that is
    #: not the account the files face itself runs as.
    account: str
    #: The box's processes, for a provider that declares
    #: ``supports_process_sessions``; ``None`` leaves those clauses out.
    processes: SandboxProcesses | None = None


@dataclass(frozen=True, slots=True)
class DataContractCheck:
    """One clause of the contract; ``run`` gets the box and an empty directory."""

    name: str
    run: Callable[[DataContractBox, str], Awaitable[None]]


async def _make_symlink(box: DataContractBox, link: str, target: str) -> None:
    """Create a symlink with a command; the files face has no call for one."""

    result = await box.commands.run(f"ln -s {shlex.quote(target)} {shlex.quote(link)}")
    assert result.exit_code == 0, f"ln -s failed: {result.output}"


async def _read_streamed(files: SandboxFiles, path: str) -> bytes:
    stream = files.read_chunks(path)
    try:
        return b"".join([chunk async for chunk in stream])
    finally:
        await stream.aclose()


async def _bytes_come_back_exactly(box: DataContractBox, root: str) -> None:
    body = b"\x00binary\xff\xfe\r\nwith a trailing newline\n"
    path = f"{root}/exact.bin"
    await box.files.write_bytes(path, body)
    assert await box.files.read_bytes(path) == body, "read_bytes changed the bytes"
    assert await _read_streamed(box.files, path) == body, "read_chunks changed the bytes"


async def _a_stream_is_written_whole(box: DataContractBox, root: str) -> None:
    body = bytes(range(256)) * 512
    path = f"{root}/streamed.bin"
    await box.files.write_bytes(path, io.BytesIO(body))
    assert await box.files.read_bytes(path) == body, "a streamed write lost or changed bytes"


async def _a_write_sets_mode_and_owner(box: DataContractBox, root: str) -> None:
    path = f"{root}/owned.txt"
    await box.files.write_bytes(
        path, b"owned", mode=0o640, owner=box.account, group=box.account
    )
    entry = (await box.files.stat([path]))[path]
    assert entry.kind == "file", entry
    assert entry.size == len(b"owned"), entry
    assert entry.mode == 0o640, f"mode is {oct(entry.mode)}, not 0o640"
    assert (entry.owner, entry.group) == (box.account, box.account), entry
    assert entry.modified_at is not None, entry


async def _a_write_replaces_the_whole_file(box: DataContractBox, root: str) -> None:
    path = f"{root}/replaced.txt"
    await box.files.write_bytes(path, b"a much longer first version")
    await box.files.write_bytes(path, b"short", mode=0o600)
    assert await box.files.read_bytes(path) == b"short", "the old content's tail survived"
    entry = (await box.files.stat([path]))[path]
    assert entry.mode == 0o600, f"a rewrite left mode {oct(entry.mode)}"


async def _a_write_creates_missing_parents(box: DataContractBox, root: str) -> None:
    path = f"{root}/a/b/leaf.txt"
    await box.files.write_bytes(path, b"leaf", owner=box.account, group=box.account)
    entries = await box.files.stat([f"{root}/a", f"{root}/a/b"])
    for parent, entry in entries.items():
        assert entry.kind == "directory", (parent, entry)
        assert entry.owner == box.account, f"created parent {parent} is owned by {entry.owner}"
    assert await box.files.read_bytes(path) == b"leaf"


async def _a_large_file_streams_and_can_stop_early(box: DataContractBox, root: str) -> None:
    body = bytes((index * 7) % 251 for index in range(3 * 1024 * 1024 + 1))
    path = f"{root}/large.bin"
    await box.files.write_bytes(path, body)
    assert await _read_streamed(box.files, path) == body, "the streamed large file differs"
    stream = box.files.read_chunks(path)
    try:
        first = await anext(stream)
        assert first and body.startswith(first), "the first chunk is not the file's start"
    finally:
        await stream.aclose()


async def _a_missing_path_is_not_found(box: DataContractBox, root: str) -> None:
    missing = f"{root}/missing.txt"
    for attempt in (
        lambda: box.files.stat([missing]),
        lambda: box.files.read_bytes(missing),
        lambda: _read_streamed(box.files, missing),
    ):
        try:
            await attempt()
        except SandboxPathNotFound:
            continue
        raise AssertionError("a missing path did not raise SandboxPathNotFound")


async def _make_directories_sets_mode_owner_once(box: DataContractBox, root: str) -> None:
    leaf = f"{root}/x/y/z"
    await box.files.make_directories(
        [SandboxDirectorySpec(path=leaf, mode=0o750, owner=box.account, group=box.account)]
    )
    entries = await box.files.stat([f"{root}/x", leaf])
    assert entries[leaf].kind == "directory", entries[leaf]
    assert entries[leaf].mode == 0o750, f"the named directory has mode {oct(entries[leaf].mode)}"
    for path, entry in entries.items():
        assert entry.owner == box.account, f"created directory {path} is owned by {entry.owner}"
    await box.files.make_directories([SandboxDirectorySpec(path=leaf, mode=0o700)])
    again = (await box.files.stat([leaf]))[leaf]
    assert again.mode == 0o750, "an existing directory took the second call's mode"


async def _a_listing_is_one_level(box: DataContractBox, root: str) -> None:
    directory = f"{root}/listed"
    await box.files.write_bytes(f"{directory}/top.txt", b"top")
    await box.files.write_bytes(f"{directory}/sub/deep.txt", b"deep")
    entries = {entry.path: entry.kind for entry in await box.files.list_directory(directory)}
    assert entries == {
        f"{directory}/top.txt": "file",
        f"{directory}/sub": "directory",
    }, entries


async def _symlinks_are_reported_not_followed(box: DataContractBox, root: str) -> None:
    await box.files.write_bytes(f"{root}/target-dir/inside.txt", b"inside")
    await box.files.write_bytes(f"{root}/target-file", b"file")
    links = {
        f"{root}/link-to-dir": f"{root}/target-dir",
        f"{root}/link-to-file": f"{root}/target-file",
        f"{root}/link-to-nothing": f"{root}/nothing-here",
    }
    for link, target in links.items():
        await _make_symlink(box, link, target)
    entries = await box.files.stat(list(links))
    for link, entry in entries.items():
        assert entry.kind == "symlink", f"stat followed {link}: {entry.kind}"
    listed = {entry.path: entry.kind for entry in await box.files.list_directory(root)}
    for link in links:
        assert listed.get(link) == "symlink", f"the listing reported {link} as {listed.get(link)}"


async def _search_matches_names_below_the_root(box: DataContractBox, root: str) -> None:
    base = f"{root}/searched"
    await box.files.write_bytes(f"{base}/a_key.jsonl", b"a")
    await box.files.write_bytes(f"{base}/deep/b_key.jsonl", b"b")
    await box.files.write_bytes(f"{base}/c_key.txt", b"c")
    await box.files.make_directories([SandboxDirectorySpec(path=f"{base}/d_key.jsonl")])
    found = sorted(await box.files.search(base, "*_key.jsonl"))
    assert found == [f"{base}/a_key.jsonl", f"{base}/deep/b_key.jsonl"], found


async def _a_move_renames_and_refuses_to_replace(box: DataContractBox, root: str) -> None:
    await box.files.write_bytes(f"{root}/first.txt", b"moved")
    await box.files.move(f"{root}/first.txt", f"{root}/new/parent/second.txt")
    assert await box.files.read_bytes(f"{root}/new/parent/second.txt") == b"moved"
    try:
        await box.files.stat([f"{root}/first.txt"])
    except SandboxPathNotFound:
        pass
    else:
        raise AssertionError("the moved source still exists")
    await box.files.write_bytes(f"{root}/occupied.txt", b"kept")
    try:
        await box.files.move(f"{root}/new/parent/second.txt", f"{root}/occupied.txt")
    except SandboxPathRejected:
        pass
    else:
        raise AssertionError("a move replaced an existing destination")
    assert await box.files.read_bytes(f"{root}/occupied.txt") == b"kept"
    try:
        await box.files.move(f"{root}/absent.txt", f"{root}/elsewhere.txt")
    except SandboxPathNotFound:
        pass
    else:
        raise AssertionError("moving a missing source did not raise SandboxPathNotFound")


async def _remove_is_recursive_and_keeps_link_targets(box: DataContractBox, root: str) -> None:
    await box.files.write_bytes(f"{root}/kept/inside.txt", b"kept")
    await _make_symlink(box, f"{root}/link", f"{root}/kept")
    await _make_symlink(box, f"{root}/dangling", f"{root}/nowhere")
    await box.files.write_bytes(f"{root}/tree/a/b.txt", b"b")
    await box.files.write_bytes(f"{root}/plain.txt", b"plain")
    await box.files.remove(
        [f"{root}/link", f"{root}/dangling", f"{root}/tree", f"{root}/plain.txt", f"{root}/absent"]
    )
    remaining = {entry.path for entry in await box.files.list_directory(root)}
    assert remaining == {f"{root}/kept"}, remaining
    assert await box.files.read_bytes(f"{root}/kept/inside.txt") == b"kept", (
        "removing a symlink removed what it points to"
    )


async def _paths_are_taken_literally(box: DataContractBox, root: str) -> None:
    try:
        await box.files.read_bytes("relative/path.txt")
    except ValueError:
        pass
    else:
        raise AssertionError("a relative path was accepted")
    literal = f"{root}/literal-$HOME-name"
    try:
        await box.files.write_bytes(literal, b"literal")
    except SandboxPathRejected:
        return
    listed = {entry.path for entry in await box.files.list_directory(root)}
    assert listed == {literal}, f"a '$' path was expanded instead of taken literally: {listed}"


async def _a_command_is_a_shell_line_run_as_root(box: DataContractBox, root: str) -> None:
    result = await box.commands.run(f"cd {shlex.quote(root)} && pwd && echo x | tr x y && id -u")
    assert (result.exit_code, result.stdout) == (0, f"{root}\ny\n0"), result


async def _an_exit_status_is_data(box: DataContractBox, root: str) -> None:
    assert (await box.commands.run("true")).exit_code == 0
    failed = await box.commands.run("echo before; exit 7")
    assert (failed.exit_code, failed.stdout) == (7, "before"), failed


async def _stdout_and_stderr_stay_apart(box: DataContractBox, root: str) -> None:
    result = await box.commands.run("echo out; echo err >&2; echo out2")
    assert (result.stdout, result.stderr) == ("out\nout2", "err"), result


async def _interior_lines_are_kept(box: DataContractBox, root: str) -> None:
    result = await box.commands.run("printf 'a\\n\\nb\\n'")
    assert result.stdout == "a\n\nb", f"the lines came back as {result.stdout!r}"


async def _the_environment_is_passed_literally(box: DataContractBox, root: str) -> None:
    value = "a b $HOME 'single' \"double\""
    result = await box.commands.run('printf %s "$CONTRACT_VALUE"', env={"CONTRACT_VALUE": value})
    assert result.stdout == value, f"the environment arrived as {result.stdout!r}"


async def _stdin_is_empty(box: DataContractBox, root: str) -> None:
    result = await box.commands.run("cat; echo done", timeout=20.0)
    assert (result.exit_code, result.stdout) == (0, "done"), result


async def _a_signal_is_a_negative_status(box: DataContractBox, root: str) -> None:
    result = await box.commands.run("kill -9 $$")
    assert result.exit_code < 0, f"a killed process reported {result.exit_code}"


async def _a_deadline_raises_timeout_and_stops_the_command(
    box: DataContractBox, root: str
) -> None:
    marker = f"{root}/survived"
    started = time.monotonic()
    try:
        await box.commands.run(f"sleep 9 && touch {shlex.quote(marker)}", timeout=1.0)
    except TimeoutError:
        pass
    else:
        raise AssertionError("a command past its deadline returned")
    assert time.monotonic() - started < 5.0, "the deadline was not kept on the caller's side"
    # Past the point where the command would have written the marker.
    await asyncio.sleep(max(0.0, started + 11.0 - time.monotonic()))
    try:
        await box.files.stat([marker])
    except SandboxPathNotFound:
        return
    raise AssertionError("the box kept running a command its caller stopped waiting for")


def _processes(box: DataContractBox) -> SandboxProcesses:
    if box.processes is None:
        raise ValueError("the box was handed to the suite without a processes face")
    return box.processes


async def _read_until(
    frames: AsyncGenerator[ProcessFrame, None],
    done: Callable[[list[ProcessFrame]], bool],
    *,
    timeout: float = 30.0,
) -> list[ProcessFrame]:
    """Read frames until ``done`` holds for what arrived, or the frames end."""

    seen: list[ProcessFrame] = []

    async def read() -> None:
        async for frame in frames:
            seen.append(frame)
            if done(seen):
                return

    try:
        await asyncio.wait_for(read(), timeout)
    except TimeoutError:
        raise AssertionError(f"the frames did not arrive within {timeout:g}s: {seen!r}") from None
    return seen


def _written(frames: Sequence[ProcessFrame], stream: str | None = None) -> bytes:
    """One stream's live output, or, without a stream, every byte including replays."""

    parts: list[bytes] = []
    for frame in frames:
        if isinstance(frame, ProcessOutput) and stream in (None, frame.stream):
            parts.append(frame.data)
        elif isinstance(frame, ProcessReplay) and stream is None:
            parts.append(frame.data)
    return b"".join(parts)


def _exited(frames: list[ProcessFrame]) -> bool:
    return isinstance(frames[-1], ProcessExit)


async def _a_process_reads_stdin_and_reports_its_exit(box: DataContractBox, root: str) -> None:
    processes = _processes(box)
    process_id = await processes.start(
        "read line; pwd; printf 'out:%s\\n' \"$line\"; printf 'err\\n' >&2; exit 3", cwd=root
    )
    try:
        attachment = await processes.attach(process_id, since=0, takeover=False)
        frames = attachment.frames()
        try:
            await attachment.send(b"hello\n")
            seen = await _read_until(frames, _exited)
        finally:
            await frames.aclose()
            await attachment.detach()
    finally:
        await processes.close(process_id)
    assert _written(seen, "stdout") == f"{root}\nout:hello\n".encode(), seen
    assert _written(seen, "stderr") == b"err\n", seen
    assert seen[-1] == ProcessExit(exit_code=3), f"the exit is not the last frame: {seen!r}"


async def _a_reattach_replays_from_an_offset(box: DataContractBox, root: str) -> None:
    processes = _processes(box)
    process_id = await processes.start(
        "read a; printf 'first\\n'; read b; printf 'second\\n'; read c", cwd=root
    )
    try:
        first = await processes.attach(process_id, since=0, takeover=False)
        frames = first.frames()
        await first.send(b"1\n")
        await _read_until(frames, lambda seen: _written(seen) == b"first\n")
        await frames.aclose()
        await first.detach()
        assert await processes.output_offset(process_id) == len(b"first\n")

        second = await processes.attach(process_id, since=0, takeover=True)
        frames = second.frames()
        await second.send(b"2\n")
        seen = await _read_until(frames, lambda seen: _written(seen) == b"first\nsecond\n")
        await frames.aclose()
        await second.detach()
        assert seen[0] == ProcessReplay(offset=0, data=b"first\n"), seen

        third = await processes.attach(process_id, since=len(b"first\n"), takeover=True)
        frames = third.frames()
        seen = await _read_until(frames, lambda seen: _written(seen) == b"second\n")
        await frames.aclose()
        await third.detach()
        assert seen[0] == ProcessReplay(offset=len(b"first\n"), data=b"second\n"), seen
    finally:
        await processes.close(process_id)


async def _one_attachment_holds_a_process(box: DataContractBox, root: str) -> None:
    processes = _processes(box)
    process_id = await processes.start("printf 'ready\\n'; read x", cwd=root)
    try:
        holder = await processes.attach(process_id, since=0, takeover=False)
        held = holder.frames()
        await _read_until(held, lambda seen: b"ready\n" in _written(seen))
        try:
            await processes.attach(process_id, since=0, takeover=False)
        except SandboxProcessAlreadyAttached:
            pass
        else:
            raise AssertionError("a second attachment was accepted without taking over")
        taker = await processes.attach(process_id, since=0, takeover=True)
        try:
            await asyncio.wait_for(anext(held), 20.0)
        except SandboxProcessDetached:
            pass
        else:
            raise AssertionError("the attachment that was taken over went on reading")
        finally:
            await held.aclose()
            await holder.detach()
            await taker.detach()
    finally:
        await processes.close(process_id)


async def _a_replay_never_starts_past_its_cursor(box: DataContractBox, root: str) -> None:
    """More output than a box may hold, then a replay from the start.

    A box that kept it all replays from offset 0; one that dropped some must
    say so with :class:`SandboxProcessReplayGap` rather than skip it.
    """

    processes = _processes(box)
    size = 3 * 1024 * 1024
    process_id = await processes.start(
        f"read x; head -c {size} /dev/zero | tr '\\0' x; read y", cwd=root
    )
    try:
        attachment = await processes.attach(process_id, since=0, takeover=False)
        await attachment.send(b"go\n")
        await attachment.detach()
        deadline = time.monotonic() + 60.0
        while await processes.output_offset(process_id) < size:
            assert time.monotonic() < deadline, "the process never wrote all its output"
            await asyncio.sleep(0.2)
        try:
            replaying = await processes.attach(process_id, since=0, takeover=True)
        except SandboxProcessReplayGap as gap:
            assert (gap.requested, gap.available > 0) == (0, True), gap
            return
        frames = replaying.frames()
        try:
            first = await asyncio.wait_for(anext(frames), 20.0)
        finally:
            await frames.aclose()
            await replaying.detach()
        assert isinstance(first, ProcessReplay) and first.offset == 0, (
            f"a replay asked for from 0 started with {first!r}"
        )
    finally:
        await processes.close(process_id)


async def _a_signal_reaches_only_a_running_process(box: DataContractBox, root: str) -> None:
    processes = _processes(box)
    marker = f"{root}/interrupted"
    process_id = await processes.start(
        f"trap 'touch {shlex.quote(marker)}; exit 9' INT; printf 'ready\\n'; "
        "while :; do sleep 0.2; done",
        cwd=root,
    )
    try:
        attachment = await processes.attach(process_id, since=0, takeover=False)
        frames = attachment.frames()
        await _read_until(frames, lambda seen: b"ready\n" in _written(seen))
        await frames.aclose()
        await attachment.detach()
        await processes.signal(process_id, "SIGINT")
        deadline = time.monotonic() + 20.0
        while True:
            try:
                await box.files.stat([marker])
                break
            except SandboxPathNotFound:
                assert time.monotonic() < deadline, "SIGINT never reached the process"
                await asyncio.sleep(0.2)
        # The trap exits right after the marker; give it the moment to.
        await asyncio.sleep(1.0)
        offset = await processes.output_offset(process_id)
        await processes.signal(process_id, "SIGINT")
        await asyncio.sleep(1.0)
        assert await processes.output_offset(process_id) == offset, (
            "signalling a process that had exited ran it again"
        )
    finally:
        await processes.close(process_id)


async def _close_stops_a_process_and_forgets_it(box: DataContractBox, root: str) -> None:
    processes = _processes(box)
    marker = f"{root}/survived"
    process_id = await processes.start(
        f"printf 'ready\\n'; sleep 3; touch {shlex.quote(marker)}", cwd=root
    )
    attachment = await processes.attach(process_id, since=0, takeover=False)
    frames = attachment.frames()
    await _read_until(frames, lambda seen: b"ready\n" in _written(seen))
    await frames.aclose()
    await attachment.detach()
    await processes.close(process_id)
    await processes.close(process_id)
    assert await processes.exists(process_id) is False, "a closed process is still known"
    for attempt in (
        lambda: processes.output_offset(process_id),
        lambda: processes.attach(process_id, since=0, takeover=True),
    ):
        try:
            await attempt()
        except SandboxProcessDetached:
            continue
        raise AssertionError("a closed process did not raise SandboxProcessDetached")
    await asyncio.sleep(4.0)
    try:
        await box.files.stat([marker])
    except SandboxPathNotFound:
        return
    raise AssertionError("a closed process kept running")


async def _an_exited_process_runs_again_when_attached(box: DataContractBox, root: str) -> None:
    processes = _processes(box)
    process_id = await processes.start("printf 'run\\n'", cwd=root)
    try:
        for since in (0, len(b"run\n")):
            attachment = await processes.attach(process_id, since=since, takeover=True)
            frames = attachment.frames()
            try:
                seen = await _read_until(frames, _exited)
            finally:
                await frames.aclose()
                await attachment.detach()
            assert (_written(seen), seen[-1]) == (b"run\n", ProcessExit(exit_code=0)), seen
    finally:
        await processes.close(process_id)


#: The files clauses, in the order :func:`check_data_contract` runs them.
FILES_CONTRACT: tuple[DataContractCheck, ...] = (
    DataContractCheck("bytes_come_back_exactly", _bytes_come_back_exactly),
    DataContractCheck("a_stream_is_written_whole", _a_stream_is_written_whole),
    DataContractCheck("a_write_sets_mode_and_owner", _a_write_sets_mode_and_owner),
    DataContractCheck("a_write_replaces_the_whole_file", _a_write_replaces_the_whole_file),
    DataContractCheck("a_write_creates_missing_parents", _a_write_creates_missing_parents),
    DataContractCheck(
        "a_large_file_streams_and_can_stop_early", _a_large_file_streams_and_can_stop_early
    ),
    DataContractCheck("a_missing_path_is_not_found", _a_missing_path_is_not_found),
    DataContractCheck(
        "make_directories_sets_mode_owner_once", _make_directories_sets_mode_owner_once
    ),
    DataContractCheck("a_listing_is_one_level", _a_listing_is_one_level),
    DataContractCheck(
        "symlinks_are_reported_not_followed", _symlinks_are_reported_not_followed
    ),
    DataContractCheck(
        "search_matches_names_below_the_root", _search_matches_names_below_the_root
    ),
    DataContractCheck(
        "a_move_renames_and_refuses_to_replace", _a_move_renames_and_refuses_to_replace
    ),
    DataContractCheck(
        "remove_is_recursive_and_keeps_link_targets",
        _remove_is_recursive_and_keeps_link_targets,
    ),
    DataContractCheck("paths_are_taken_literally", _paths_are_taken_literally),
)

#: The commands clauses, in the order :func:`check_data_contract` runs them.
COMMANDS_CONTRACT: tuple[DataContractCheck, ...] = (
    DataContractCheck(
        "a_command_is_a_shell_line_run_as_root", _a_command_is_a_shell_line_run_as_root
    ),
    DataContractCheck("an_exit_status_is_data", _an_exit_status_is_data),
    DataContractCheck("stdout_and_stderr_stay_apart", _stdout_and_stderr_stay_apart),
    DataContractCheck("interior_lines_are_kept", _interior_lines_are_kept),
    DataContractCheck(
        "the_environment_is_passed_literally", _the_environment_is_passed_literally
    ),
    DataContractCheck("stdin_is_empty", _stdin_is_empty),
    DataContractCheck("a_signal_is_a_negative_status", _a_signal_is_a_negative_status),
    DataContractCheck(
        "a_deadline_raises_timeout_and_stops_the_command",
        _a_deadline_raises_timeout_and_stops_the_command,
    ),
)

#: Every files and commands clause, files first.
DATA_CONTRACT: tuple[DataContractCheck, ...] = FILES_CONTRACT + COMMANDS_CONTRACT

#: The processes clauses, for a provider that declares ``supports_process_sessions``.
PROCESSES_CONTRACT: tuple[DataContractCheck, ...] = (
    DataContractCheck(
        "a_process_reads_stdin_and_reports_its_exit",
        _a_process_reads_stdin_and_reports_its_exit,
    ),
    DataContractCheck("a_reattach_replays_from_an_offset", _a_reattach_replays_from_an_offset),
    DataContractCheck("one_attachment_holds_a_process", _one_attachment_holds_a_process),
    DataContractCheck(
        "a_replay_never_starts_past_its_cursor", _a_replay_never_starts_past_its_cursor
    ),
    DataContractCheck(
        "a_signal_reaches_only_a_running_process", _a_signal_reaches_only_a_running_process
    ),
    DataContractCheck(
        "close_stops_a_process_and_forgets_it", _close_stops_a_process_and_forgets_it
    ),
    DataContractCheck(
        "an_exited_process_runs_again_when_attached",
        _an_exited_process_runs_again_when_attached,
    ),
)


async def run_contract_check(check: DataContractCheck, box: DataContractBox) -> None:
    """Run one check in its own empty directory under ``box.scratch``, then remove it."""

    root = posixpath.join(box.scratch, check.name)
    await box.files.make_directories([SandboxDirectorySpec(path=root)])
    try:
        await check.run(box, root)
    finally:
        await box.files.remove([root])


async def check_data_contract(box: DataContractBox) -> list[str]:
    """Run every check; return their names, or raise naming each one that failed.

    The processes clauses run when the box has a ``processes`` face.
    """

    failures: list[str] = []
    passed: list[str] = []
    checks = DATA_CONTRACT + (PROCESSES_CONTRACT if box.processes is not None else ())
    try:
        for check in checks:
            try:
                await run_contract_check(check, box)
            except Exception as exc:  # noqa: BLE001 - every clause is reported
                failures.append(f"{check.name}: {type(exc).__name__}: {exc}")
            else:
                passed.append(check.name)
    finally:
        await box.files.remove([box.scratch])
    if failures:
        raise AssertionError("data contract failed:\n" + "\n".join(failures))
    return passed


__all__ = [
    "COMMANDS_CONTRACT",
    "DATA_CONTRACT",
    "DataContractBox",
    "DataContractCheck",
    "FILES_CONTRACT",
    "PROCESSES_CONTRACT",
    "check_data_contract",
    "run_contract_check",
]
