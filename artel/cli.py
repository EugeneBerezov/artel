import argparse
import importlib
import sys

from artel import __version__
from artel.errors import ArtelError, NotAvailable

COMMANDS = {
    "init": "artel.cmd_init:run",
    "add": "artel.cmd_agent:run_add",
    "note": "artel.cmd_agent:run_note",
    "heavy": "artel.cmd_agent:run_heavy",
    "status": "artel.cmd_human:run_status",
    "show": "artel.cmd_human:run_show",
    "logs": "artel.cmd_human:run_logs",
    "stats": "artel.cmd_stats:run",
    "answer": "artel.requests:run_answer",
    "approve": "artel.requests:run_approve",
    "reject": "artel.requests:run_reject",
    "accept": "artel.requests:run_accept",
    "rework": "artel.requests:run_rework",
    "pause": "artel.requests:run_pause",
    "resume": "artel.requests:run_resume",
    "stop": "artel.requests:run_stop",
    "run": "artel.runner:run_command",
    "notify": "artel.notify:run_command",
    "config": "artel.cmd_config:run",
    "templates": "artel.templates:run_command",
}


class _Parser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        ns = super().parse_args(args, namespace)
        if getattr(ns, "command", None) == "heavy" and ns.cmd[:1] == ["--"]:
            ns.cmd = ns.cmd[1:]
        return ns


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="artel", description="Run coding agents in parallel on one git repository.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("init", help="set up artel in this repository")
    s.add_argument("--force", action="store_true")
    s.add_argument("--yes", action="store_true")

    s = sub.add_parser("add", help="add a task")
    s.add_argument("title")
    s.add_argument("--body")
    s.add_argument("--area", action="append", default=[])
    s.add_argument("--priority", type=int, default=0)
    s.add_argument("--after", type=int, action="append", default=[])
    s.add_argument("--attack", action="store_true")
    s.add_argument("--strong", action="store_true")
    s.add_argument("--review", type=int)
    s.add_argument("--merge", choices=["auto", "ask"])

    s = sub.add_parser("note", help="append a journal entry")
    s.add_argument("kind", choices=["FACT", "FAIL"])
    s.add_argument("text")
    s.add_argument("--basis")

    s = sub.add_parser("heavy", help="run a heavy command under the heavy lock")
    s.add_argument("cmd", nargs=argparse.REMAINDER)

    for name, helptext in (("status", "show the board"), ("stats", "show statistics")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--json", action="store_true")
    for name in ("show", "logs"):
        s = sub.add_parser(name, help=f"{name} a task")
        s.add_argument("id", type=int)

    s = sub.add_parser("answer", help="answer a task's question")
    s.add_argument("id", type=int)
    s.add_argument("text")
    for name in ("approve", "accept"):
        s = sub.add_parser(name, help=f"{name} a task")
        s.add_argument("id", type=int)
    s = sub.add_parser("reject", help="reject a task")
    s.add_argument("id", type=int)
    s.add_argument("--reason")
    s = sub.add_parser("rework", help="send a task back for rework")
    s.add_argument("id", type=int)
    s.add_argument("--note", required=True)

    s = sub.add_parser("run", help="run the runner")
    s.add_argument("-n", type=int)
    s = sub.add_parser("stop", help="stop the runner")
    s.add_argument("--now", action="store_true")
    sub.add_parser("pause", help="pause picking new work")
    s = sub.add_parser("resume", help="resume picking new work")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("notify", help="notifier tools")
    s.add_argument("notify_cmd", choices=["test"])

    s = sub.add_parser("config", help="show or change settings")
    s.add_argument("config_cmd", nargs="?", choices=["explain", "set"])
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")

    s = sub.add_parser("templates", help="prompt templates")
    s.add_argument("templates_cmd", choices=["export"])
    s.add_argument("--force", action="store_true")
    return p


def _resolve(spec: str):
    module_name, _, func_name = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # only "the target itself is missing"; a broken import inside it is a real bug
        if exc.name and (module_name == exc.name or module_name.startswith(exc.name + ".")):
            return None
        raise
    return getattr(module, func_name, None)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        handler = _resolve(COMMANDS[args.command])
        if handler is None:
            raise NotAvailable(f"artel {args.command}: not available in this build")
        return handler(args)
    except ArtelError as exc:
        print(f"artel: {exc}", file=sys.stderr)
        return exc.exit_code
