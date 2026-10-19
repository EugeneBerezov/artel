"""Every OS-specific call in artel lives here (spec §12)."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from artel.errors import ArtelError

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl


class FileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def _try(self) -> bool:
        try:
            if IS_WINDOWS:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def acquire(self, blocking: bool = False) -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        while True:
            if self._try():
                return True
            if not blocking:
                self._fh.close()
                self._fh = None
                return False
            time.sleep(0.1)

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if IS_WINDOWS:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


# --- Windows kernel32 plumbing -------------------------------------------------------------

_JOBS: dict[int, int] = {}   # pid -> job handle (Windows only)

if IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.GetProcessTimes.restype = wintypes.BOOL

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _STILL_ACTIVE = 259
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in
                    ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def popen_kwargs() -> dict:
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def register_tree(proc: subprocess.Popen) -> None:
    if not IS_WINDOWS:
        return
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        return
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    _k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                 ctypes.sizeof(info))
    handle = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, proc.pid)
    if handle:
        try:
            if not _k32.AssignProcessToJobObject(job, handle):
                _k32.CloseHandle(job)
                return
        finally:
            _k32.CloseHandle(handle)
    _JOBS[proc.pid] = job


def group_of(proc: subprocess.Popen) -> int:
    if IS_WINDOWS:
        return proc.pid
    try:
        return os.getpgid(proc.pid)
    except OSError:
        return proc.pid   # already reaped: start_new_session made pgid == pid


def soft_terminate(pid: int, group: int) -> None:
    try:
        if IS_WINDOWS:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(group, signal.SIGTERM)
    except OSError:
        pass


def hard_kill(pid: int, group: int) -> None:
    if IS_WINDOWS:
        job = _JOBS.pop(pid, None)
        if job:
            _k32.TerminateJobObject(job, 1)
            _k32.CloseHandle(job)
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                       encoding="utf-8", errors="ignore")
        return
    try:
        os.killpg(group, signal.SIGKILL)
    except OSError:
        pass


def tree_alive(pid: int, group: int) -> bool:
    if IS_WINDOWS:
        return pid_alive(pid)
    try:
        os.killpg(group, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(_k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return _not_zombie(pid)


def _not_zombie(pid: int) -> bool:
    """An exited-but-unreaped child still answers kill(pid, 0); treat it as gone where we can tell."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return text.rpartition(")")[2].split()[0] != "Z"


def process_fingerprint(pid: int) -> str | None:
    if IS_WINDOWS:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not _k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            _k32.CloseHandle(handle)
    stat = Path(f"/proc/{pid}/stat")
    if sys.platform.startswith("linux"):
        try:
            text = stat.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        fields = text.rpartition(")")[2].split()   # fields[0] is field 3 (state)
        if fields[0] == "Z":
            return None
        return fields[19]                           # field 22: starttime
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                         encoding="utf-8", errors="replace")
    value = out.stdout.strip()
    return value or None


def terminate_tree(pid: int, group: int, grace: float, fingerprint: str | None = None) -> bool:
    current = process_fingerprint(pid)
    if current is None:
        return False
    if fingerprint is not None and current != fingerprint:
        return False
    soft_terminate(pid, group)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not tree_alive(pid, group):
            return True
        time.sleep(0.05)
    if tree_alive(pid, group):
        hard_kill(pid, group)
    return True


def install_stop_handlers(callback: Callable[[], None]) -> None:
    names = ("SIGINT", "SIGBREAK") if IS_WINDOWS else ("SIGINT", "SIGTERM")
    for name in names:
        signal.signal(getattr(signal, name), lambda signum, frame: callback())


def which(name: str) -> str | None:
    return shutil.which(name)


def resolve_argv(argv: Sequence[str]) -> list[str]:
    found = which(argv[0])
    if found is None:
        raise ArtelError(f"cannot find {argv[0]} on PATH")
    return [found, *argv[1:]]


def git_bash_candidates(exec_path: str, env: Mapping[str, str]) -> list[Path]:
    out: list[Path] = []
    if env.get("CLAUDE_CODE_GIT_BASH_PATH"):
        out.append(Path(env["CLAUDE_CODE_GIT_BASH_PATH"]))
    for ancestor in Path(exec_path).parents:
        for rel in (("bin", "bash.exe"), ("usr", "bin", "bash.exe")):
            candidate = ancestor.joinpath(*rel)
            if candidate.exists():
                out.append(candidate)
    return out


def find_git_bash() -> str:
    exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, encoding="utf-8",
                               errors="replace").stdout.strip()
    for candidate in git_bash_candidates(exec_path, os.environ):
        if candidate.exists():
            return str(candidate)
    found = which("bash")
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    if found and system32 not in Path(found).parents:
        return found
    raise ArtelError("cannot find bash; install Git for Windows (https://gitforwindows.org)")


def shell_argv(shell: str, command: str, *, windows: bool = IS_WINDOWS) -> list[str]:
    if shell == "auto":
        return [find_git_bash(), "-c", command] if windows else ["sh", "-c", command]
    if shell in ("sh", "bash"):
        return [which(shell) or shell, "-c", command]
    if shell == "pwsh":
        return ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command]
    if shell == "cmd":
        return ["cmd", "/d", "/s", "/c", command]
    raise ArtelError(f"unknown shell {shell!r} (expected auto, sh, bash, pwsh or cmd)")


def prefix_argv(shell: str, prefix: str, command_argv: Sequence[str], *,
                windows: bool = IS_WINDOWS) -> list[str]:
    if shell in ("pwsh", "cmd"):
        raise ArtelError("commands.heavy_lock must be a list when shell is pwsh or cmd")
    return [*shell_argv(shell, prefix + ' "$@"', windows=windows), "artel-heavy", *command_argv]


def shim_files(python: str, *, windows: bool = IS_WINDOWS) -> dict[str, str]:
    files = {"artel": f'#!/bin/sh\nexec "{python}" -m artel "$@"\n'}
    if windows:
        files["artel.cmd"] = f'@"{python}" -m artel %*\r\n'
    return files


def write_shim(bin_dir: Path, python: str) -> Path:
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, content in shim_files(python).items():
        with open(bin_dir / name, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
    if not IS_WINDOWS:
        (bin_dir / "artel").chmod(0o755)
    return bin_dir


def shim_invocation(bin_dir: Path) -> list[str]:
    return [str(Path(bin_dir) / ("artel.cmd" if IS_WINDOWS else "artel"))]


def write_wrapper(stem: Path, argv: Sequence[str]) -> list[str]:
    stem = Path(stem)
    if IS_WINDOWS:
        path = stem.with_name(stem.name + ".cmd")
        body = "@" + " ".join(f'"{a}"' for a in argv) + " %*\r\n"
    else:
        path = stem
        body = "exec " + " ".join(f'"{a}"' for a in argv) + ' "$@"\n'
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(body if IS_WINDOWS else "#!/bin/sh\n" + body)
    if not IS_WINDOWS:
        path.chmod(0o755)
    return [str(path)]


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def long_paths_warning(repo_git: Callable[..., str], *, windows: bool = IS_WINDOWS) -> str | None:
    if not windows:
        return None
    if repo_git("config", "core.longpaths").strip() == "true":
        return None
    return "git core.longpaths is not enabled; deep paths may fail. Fix: git config core.longpaths true"
