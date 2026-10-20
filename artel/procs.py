"""Run a command as a process tree: stream its output, enforce a timeout, stop it on request (spec §5, §12).

Portable: every OS-specific call lives in artel.platform.
"""
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from artel import platform
from artel.errors import ArtelError

Command = str | list[str]          # a command setting: string → configured shell, list → direct argv
OnStart = Callable[[str, int, int, str | None], None]   # (kind, pid, group, fingerprint)

POLL = 0.05
JOIN_DEADLINE = 5.0                 # reader threads are abandoned after this


@dataclass(frozen=True)
class ProcResult:
    returncode: int | None
    timed_out: bool
    stopped: bool
    duration: float


def command_argv(cmd: Command, shell: str) -> list[str]:
    if isinstance(cmd, str):
        if not cmd.strip():
            raise ArtelError("empty command")
        return platform.shell_argv(shell, cmd)
    if not cmd or not all(isinstance(part, str) for part in cmd):
        raise ArtelError("empty command" if not cmd else f"a command list must hold strings: {cmd!r}")
    return platform.resolve_argv(cmd)


def heavy_argv(cmd: Command, heavy_lock: Command, shell: str) -> list[str]:
    argv = command_argv(cmd, shell)
    if not heavy_lock:
        return argv
    if isinstance(heavy_lock, str):
        return platform.prefix_argv(shell, heavy_lock, argv)
    return platform.resolve_argv(heavy_lock) + argv


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").removesuffix("\n").removesuffix("\r")


def _pump(stream, sink_path: Path | None, on_line, errors: list, abandoned: threading.Event) -> None:
    """Copy lines from a binary `stream` to a file and a callback until EOF. A failing log file or callback
    never stops the reading: an undrained pipe would block the child. After `abandoned` is set (run_tree
    gave up on a pipe an escaped process still holds) lines are read and dropped: nothing is written or
    reported once the caller has moved on."""
    sink = None
    if sink_path is not None:
        try:
            sink_path.parent.mkdir(parents=True, exist_ok=True)
            sink = open(sink_path, "a", encoding="utf-8", newline="\n")
        except OSError as exc:
            errors.append(exc)
    try:
        for raw in iter(stream.readline, b""):
            if abandoned.is_set():
                continue
            line = _decode(raw)
            if sink is not None:
                try:
                    sink.write(line + "\n")
                    sink.flush()
                except (OSError, ValueError) as exc:   # disk full, file gone: keep draining, report later
                    errors.append(exc)
                    sink = None
            if on_line is not None:
                try:
                    on_line(line)
                except Exception as exc:   # noqa: BLE001 - keep draining; re-raised by run_tree
                    errors.append(exc)
    except (OSError, ValueError):
        pass   # the pipe was closed under us
    finally:
        if sink is not None:
            try:
                sink.close()
            except OSError:
                pass
        if abandoned.is_set():   # run_tree left this pipe to us; release it now that the holder is gone
            try:
                stream.close()
            except OSError:
                pass


def _feed(stream, text: str) -> None:
    try:
        if text:
            stream.write(text.encode("utf-8"))   # bytes: no "\n" → "\r\n" translation on Windows
        stream.close()
    except (OSError, ValueError):
        pass   # the child closed stdin or exited


def run_tree(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None,
             stdin_text: str = "", timeout: float | None, kill_grace: float,
             log_path: Path | None = None, merge_stderr: bool = False,
             on_start: Callable[[int, int, str | None], None] | None = None,
             on_line: Callable[[str], None] | None = None,
             stop_event: threading.Event | None = None) -> ProcResult:
    started = time.monotonic()
    if not argv:
        raise ArtelError("cannot start an empty command")
    try:
        proc = subprocess.Popen(
            platform.command_line(argv), cwd=str(cwd), env=None if env is None else dict(env),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            **platform.popen_kwargs())   # binary pipes: decoded here as UTF-8, "\n" untouched
    except (OSError, ValueError) as exc:
        raise ArtelError(f"cannot start {argv[0]}: {exc}") from exc
    pid = proc.pid
    group = pid
    fingerprint = None
    timed_out = stopped = False
    errors: list[Exception] = []
    threads: list[tuple[threading.Thread, object]] = []   # (thread, the stream it owns)
    abandoned = threading.Event()
    try:
        platform.register_tree(proc)
        group = platform.group_of(proc)
        fingerprint = platform.process_fingerprint(pid)
        if on_start is not None:
            on_start(pid, group, fingerprint)
        err_path = None if log_path is None else Path(str(log_path) + ".stderr")
        pumps = [(proc.stdout, log_path, on_line)]
        if not merge_stderr:
            pumps.append((proc.stderr, err_path, None))   # drained even without a log so the child never blocks
        for stream, path, callback in pumps:
            t = threading.Thread(target=_pump, args=(stream, path, callback, errors, abandoned), daemon=True)
            t.start()
            threads.append((t, stream))
        feeder = threading.Thread(target=_feed, args=(proc.stdin, stdin_text), daemon=True)
        feeder.start()
        threads.append((feeder, proc.stdin))

        deadline = None if timeout is None else started + timeout
        while proc.poll() is None:
            if stop_event is not None and stop_event.is_set():
                stopped = True
            elif deadline is not None and time.monotonic() >= deadline:
                timed_out = True
            if (stopped or timed_out) and proc.poll() is not None:
                stopped = timed_out = False   # it finished by itself between the poll and the check
                break
            if stopped or timed_out:
                platform.terminate_tree(pid, group, kill_grace, fingerprint)
                try:
                    proc.wait(timeout=kill_grace + JOIN_DEADLINE)
                except subprocess.TimeoutExpired:
                    platform.hard_kill(pid, group, fingerprint)
                    proc.wait(timeout=JOIN_DEADLINE)
                break
            time.sleep(POLL)
        proc.wait()
        # the leader is gone; a child may still hold the pipes (e.g. a server started by `sh -c "npm test"`)
        # (unconditional: it also releases the Windows job handle; the fingerprint stops it signalling a reused pid)
        platform.terminate_tree(pid, group, kill_grace, fingerprint)
    except BaseException:
        platform.terminate_tree(pid, group, kill_grace, fingerprint)
        try:
            proc.wait(timeout=JOIN_DEADLINE)   # never leave a zombie behind
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        limit = time.monotonic() + JOIN_DEADLINE
        for t, _ in threads:
            t.join(max(0.0, limit - time.monotonic()))
        if any(t.is_alive() for t, _ in threads):
            abandoned.set()   # a pipe is still held by an escaped process: its pump goes quiet
        owned = {id(stream) for t, stream in threads if t.is_alive()}   # closing a stream in use would block
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None and id(stream) not in owned:
                try:
                    stream.close()
                except OSError:
                    pass   # stdin already broken
    if errors:
        # PD (T8 review): a log or on_line failure is fatal for the caller, after the run was fully drained
        # and the tree terminated; callers (stages, integrate) treat it as an internal error.
        raise errors[0]
    return ProcResult(returncode=proc.returncode, timed_out=timed_out, stopped=stopped,
                      duration=time.monotonic() - started)
