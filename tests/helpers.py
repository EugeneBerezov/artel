import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def py_cmd(code: str) -> list[str]:
    """Portable child process for tests: never sh/sleep/cat."""
    return [sys.executable, "-c", code]


def subprocess_env(**extra: str) -> dict[str, str]:
    """Environment for child processes that run `python -m artel` from this checkout."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if p)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def git(cwd, *args, check=True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                            encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def make_repo(root: Path, base: str = "main") -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", base)
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "README").write_text("seed\n", encoding="utf-8", newline="\n")
    git(repo, "add", "README")
    git(repo, "commit", "-q", "-m", "seed")
    return repo


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name).resolve()   # macOS /var → /private/var; Windows 8.3 names
        self.repo_path = make_repo(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def commit_file(self, rel: str, content: str, message: str = "change", cwd: Path | None = None) -> str:
        where = cwd or self.repo_path
        path = where / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        git(where, "add", rel)
        git(where, "commit", "-q", "-m", message)
        return git(where, "rev-parse", "HEAD")


IGNORE_SOFT_STOP = ("import signal\n"
                    "signal.signal(getattr(signal, 'SIGBREAK', signal.SIGTERM), signal.SIG_IGN)\n")
