import subprocess
from dataclasses import dataclass
from pathlib import Path

from artel import platform
from artel.errors import ArtelError, GitError

INTEGRATION = "artel/integration"


def task_branch(task_id: int) -> str:
    return f"artel/{task_id}"


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str | None
    branch: str | None  # without refs/heads/


def _run(args: list[str], cwd: Path, input: str | None = None, env: dict | None = None):
    try:
        # own process group / session, like agents, setup and verify: a Ctrl+C or group SIGTERM aimed at the runner
        # during a soft stop must not kill a running git
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, input=input, encoding="utf-8",
                              errors="replace", env=env, **platform.popen_kwargs())
    except FileNotFoundError as exc:
        if Path(cwd).is_dir():   # the directory exists, so the executable is what is missing
            raise ArtelError("git not found on PATH") from exc
        raise


class Repo:
    def __init__(self, root: Path, common_dir: Path):
        self.root = root
        self.common_dir = common_dir
        self.artel_dir = common_dir / "artel"
        self.board_path = self.artel_dir / "board.db"
        self.lock_path = self.artel_dir / "runner.lock"
        self.stop_path = self.artel_dir / "stop"
        self.logs_dir = self.artel_dir / "logs"
        self.tmp_dir = self.artel_dir / "tmp"

    @classmethod
    def discover(cls, start: Path) -> "Repo":
        start = Path(start)
        try:
            common = _run(["rev-parse", "--path-format=absolute", "--git-common-dir"], start)
        except (FileNotFoundError, NotADirectoryError) as exc:   # the start directory does not exist
            raise ArtelError(f"not inside a git repository ({exc})") from exc
        if common.returncode != 0:
            detail = common.stderr.strip().splitlines()
            raise ArtelError("not inside a git repository" + (f" (git: {detail[-1]})" if detail else ""))
        common_dir = Path(common.stdout.strip())
        listing = _run(["worktree", "list", "--porcelain"], start)
        if listing.returncode != 0:
            raise GitError(["worktree", "list", "--porcelain"], listing.stderr)
        root = None
        for line in listing.stdout.splitlines():
            if line.startswith("worktree "):
                root = Path(line[len("worktree "):])
                break
        if root is None:
            raise ArtelError("not inside a git repository")
        return cls(root, common_dir)

    def git(self, *args: str, cwd: Path | None = None, check: bool = True,
            input: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
        result = _run(list(args), cwd or self.root, input=input, env=env)
        if check and result.returncode != 0:
            raise GitError(list(args), result.stderr)
        return result

    def rev(self, ref: str) -> str | None:
        result = self.git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0

    def worktrees(self) -> list[Worktree]:
        out = self.git("worktree", "list", "--porcelain").stdout
        result = []
        for block in out.split("\n\n"):
            path = head = branch = None
            for line in block.splitlines():
                if line.startswith("worktree "):
                    path = Path(line[len("worktree "):])
                elif line.startswith("HEAD "):
                    head = line[len("HEAD "):]
                elif line.startswith("branch "):
                    branch = line[len("branch "):].removeprefix("refs/heads/")
            if path is not None:
                result.append(Worktree(path, head, branch))
        return result

    def branch_exists(self, branch: str) -> bool:
        return self.git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
