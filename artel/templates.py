"""Stage prompt templates and the unattended notice (spec §6, §10): built in, overridable per project."""
import importlib.resources
from pathlib import Path

from artel.config import Config, load_config
from artel.errors import ArtelError
from artel.gitutil import Repo

TEMPLATE_NAMES = ("vet.md", "attack.md", "write.md", "review.md", "review-closure.md",
                  "review-verify-fix.md", "review-conflict.md", "fix.md")
NOTICE_NAME = "unattended.md"
PLACEHOLDERS = ("task_id", "title", "body", "area", "stage", "round", "branch", "diff_range",
                "task_history", "findings", "closure_list", "verify_log", "context", "answer",
                "open_tasks", "risk_signals", "conflict_paths", "mutation_check", "required_tests")


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")   # a checkout with autocrlf must not change the cached prompt prefix


def builtin_text(name: str) -> str:
    resource = importlib.resources.files("artel.builtin_templates").joinpath(name)
    return _normalize(resource.read_text(encoding="utf-8"))


def project_path(repo: Repo, cfg: Config, name: str) -> Path:
    return repo.root / str(cfg["templates_dir"]) / name


def template_source(repo: Repo, cfg: Config, name: str) -> str:
    if project_path(repo, cfg, name).is_file():
        return f"project ({(Path(str(cfg['templates_dir'])) / name).as_posix()})"
    return "built-in"


def _read(path: Path) -> str:
    try:
        return _normalize(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ArtelError(f"{path}: not valid UTF-8 ({exc.reason}); fix or delete the file") from exc


def load_template(repo: Repo, cfg: Config, name: str) -> str:
    path = project_path(repo, cfg, name)
    if path.is_file():
        return _read(path)
    return builtin_text(name)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def notice_path(repo: Repo, cfg: Config) -> Path:
    project = project_path(repo, cfg, NOTICE_NAME)
    if project.is_file():
        return project
    target = repo.artel_dir / NOTICE_NAME
    text = builtin_text(NOTICE_NAME)
    # part of the cached prompt prefix (spike Q6): rewrite only when the content differs
    if not target.is_file() or target.read_bytes() != text.encode("utf-8"):   # byte-exact, CRLF included
        _write(target, text)
    return target


def export(repo: Repo, cfg: Config, *, force: bool = False, only_missing: bool = False) -> list[tuple[str, str]]:
    results = []
    for name in (*TEMPLATE_NAMES, NOTICE_NAME):
        path = project_path(repo, cfg, name)
        text = builtin_text(name)
        if path.exists() and not path.is_file():
            results.append((name, "kept (not a file)"))
        elif not path.is_file():
            _write(path, text)
            results.append((name, "written"))
        elif only_missing:
            results.append((name, "kept (exists)"))
        elif _read(path) == text:
            results.append((name, "unchanged"))
        elif force:
            _write(path, text)
            results.append((name, "written"))
        else:
            results.append((name, "kept (edited; use --force)"))
    return results


def run_command(args) -> int:
    repo = Repo.discover(Path.cwd())
    cfg = load_config(repo)
    for name, action in export(repo, cfg, force=bool(getattr(args, "force", False))):
        print(f"{name}: {action}")
    return 0
