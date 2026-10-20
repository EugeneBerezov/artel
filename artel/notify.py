"""Notifications (spec §7): a send-only Telegram notifier, a command notifier and the dispatcher.

Telegram uses `getMe` and `sendMessage` only; there is no code path that reads updates, so a bot another program
already uses keeps receiving its own messages. The config stores where the secret comes from, never the secret.
Message text carries task id, title, state and the question; never tokens, file contents or tracebacks.
"""
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from artel import config as config_mod, procs
from artel.errors import ArtelError
from artel.events import EVENT_KINDS, Event
from artel.gitutil import Repo

__all__ = ["EVENT_KINDS", "Event", "format_event", "TelegramCredentials", "find_telegram_credentials",
           "TelegramNotifier", "CommandNotifier", "Dispatcher", "check_and_enable", "run_command"]

TEXT_LIMIT = 3500           # Telegram refuses more than 4096 characters
COMMAND_TIMEOUT = 30.0
_TOKEN_LIKE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{25,}")   # the shape of a Telegram bot token
_STAGE = re.compile(r"internal error in (\S+?)(?:[;:,]|\s|$)")


_TRACEBACK = "Traceback (most recent call last)"


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _scrub(text: str, secrets=()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return _TOKEN_LIKE.sub("***", text)


def _when(ts: float) -> str:
    local = datetime.fromtimestamp(ts, timezone.utc).astimezone()
    utc = datetime.fromtimestamp(ts, timezone.utc)
    return f"{local:%Y-%m-%d %H:%M} (UTC{local:%z}) / {utc:%Y-%m-%d %H:%M} UTC"


def format_event(e: Event) -> str:
    """Only id, title, state and the question; an `error` never carries its text (PD-38: no traceback)."""
    who = f"#{e.task_id} " if e.task_id is not None else ""
    title = f" — {e.title}" if e.title else ""
    if e.kind == "error":
        stage = _STAGE.search(e.text or "")
        where = f" in {stage.group(1)}" if stage else ""
        tail = f"\nsee artel show {e.task_id}" if e.task_id is not None else ""
        return _clip(_scrub(f"artel: {who}internal error{where}{title}{tail}"))
    head = f"artel: {who}{e.kind.replace('_', ' ')}{title}"
    lines = [head]
    if e.text:
        lines.append(e.text.split(_TRACEBACK)[0].rstrip())   # spec §7: never a traceback, whatever the kind
    if e.resets_at is not None:
        lines.append(f"resets {_when(e.resets_at)}")
    return _clip(_scrub("\n".join(lines)))


# credentials -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TelegramCredentials:
    token: str
    chat: str
    source: str     # like "token: env ARTEL_TELEGRAM_TOKEN; chat: file /x/chat"

    def __repr__(self) -> str:   # never show the token, even in a traceback or a log line
        return f"TelegramCredentials(source={self.source!r})"


def _from_file(name: str, base: Path | None = None) -> tuple[str, str] | None:
    if not name:
        return None
    path = Path(name)
    if base is not None and not path.is_absolute():
        path = base / path          # relative to the project, not to wherever artel was started
    try:
        value = path.read_text(encoding="utf-8-sig").strip()   # utf-8-sig: Notepad writes a BOM
    except (OSError, ValueError):
        return None
    return (value, f"file {name}") if value else None


def _from_env(env: Mapping[str, str], names) -> tuple[str, str] | None:
    for name in names:
        if name and env.get(name, "").strip():
            return env[name].strip(), f"env {name}"
    return None


def find_telegram_credentials(cfg, env: Mapping[str, str]) -> TelegramCredentials | None:
    """Per value: the config file, the config env name, ARTEL_TELEGRAM_*, then TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID."""
    def look(kind: str, defaults: tuple[str, ...]):
        return (_from_file(cfg[f"notify.telegram_{kind}_file"], Path(cfg.path).parent)
                or _from_env(env, [cfg[f"notify.telegram_{kind}_env"], *defaults]))

    token = look("token", ("ARTEL_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN"))
    chat = look("chat", ("ARTEL_TELEGRAM_CHAT", "TELEGRAM_CHAT_ID"))
    if token is None or chat is None:
        return None
    return TelegramCredentials(token[0], chat[0], f"token: {token[1]}; chat: {chat[1]}")


# notifiers ---------------------------------------------------------------------------------------------------

class TelegramNotifier:
    """Only `get_me` and `send` exist."""

    def __init__(self, creds: TelegramCredentials, api_base: str, timeout: float = 10.0):
        self.creds = creds
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        host = urllib.parse.urlsplit(self.api_base).hostname or ""
        loopback = host in ("localhost", "127.0.0.1", "::1")
        # a configured proxy must not capture a test double or a local Bot API server
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if loopback \
            else urllib.request.build_opener()
        self.last_error = ""

    def _call(self, method: str, payload: dict | None):
        url = f"{self.api_base}/bot{self.creds.token}/{method}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        try:
            request = urllib.request.Request(url, data=data, method="GET" if data is None else "POST",
                                             headers={"Content-Type": "application/json"})
            with self._opener.open(request, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            self.last_error = f"HTTP {exc.code}"
            try:
                body = json.loads(exc.read().decode("utf-8", errors="replace"))
                description = body.get("description")
                if description:
                    self.last_error += f": {_scrub(str(description), [self.creds.token])}"
            except (OSError, ValueError, AttributeError):
                pass
            return None
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # URLError, timeouts, refused connections, bad JSON, a URL urllib refuses (its message embeds the URL,
            # and the URL embeds the token): never let the exception out
            if isinstance(exc, OSError):
                self.last_error = _scrub(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}", [self.creds.token])
            else:   # ValueError / InvalidURL messages quote the URL, which holds the token: name the class only
                self.last_error = f"{type(exc).__name__} (check notify.telegram_api_base and the token and chat values)"
            return None
        if not isinstance(body, dict) or not body.get("ok"):
            self.last_error = _scrub(str((body or {}).get("description", "not ok")) if isinstance(body, dict)
                                     else "unexpected reply", [self.creds.token])
            return None
        return body

    def get_me(self) -> str | None:
        body = self._call("getMe", None)
        if body is None:
            return None
        result = body.get("result")
        username = result.get("username") if isinstance(result, dict) else None
        return username if isinstance(username, str) and username else None

    def send(self, text: str) -> bool:
        return self._call("sendMessage", {"chat_id": self.creds.chat, "text": text}) is not None


class CommandNotifier:
    def __init__(self, command, shell: str):
        self.command = command
        self.shell = shell

    def send_event(self, e: Event) -> bool:
        """The event as JSON (UTF-8) on stdin; the formatted text also in ARTEL_NOTIFY_TEXT."""
        try:
            argv = procs.command_argv(self.command, self.shell)
            env = dict(os.environ, ARTEL_NOTIFY_TEXT=format_event(e))
            result = procs.run_tree(argv, cwd=Path.cwd(), env=env, stdin_text=e.to_json(),
                                    timeout=COMMAND_TIMEOUT, kill_grace=5.0)
        except (ArtelError, OSError):
            return False
        return result.returncode == 0 and not result.timed_out


class Dispatcher:
    def __init__(self, on, telegram: TelegramNotifier | None = None, command: CommandNotifier | None = None):
        self.on = set(on)
        self.telegram = telegram
        self.command = command

    @classmethod
    def from_config(cls, cfg, env: Mapping[str, str]) -> "Dispatcher":
        telegram = None
        if cfg["notify.telegram_enabled"]:
            creds = find_telegram_credentials(cfg, env)
            if creds is not None:
                telegram = TelegramNotifier(creds, cfg["notify.telegram_api_base"])
        command = None
        if cfg["notify.command"]:
            command = CommandNotifier(cfg["notify.command"], cfg["shell"])
        return cls(cfg["notify.on"], telegram, command)

    def emit(self, e: Event) -> None:
        """Filtered by notify.on; never raises; failures go to stderr (never with a secret)."""
        try:
            if e.kind not in self.on:
                return
            if self.telegram is not None and not self.telegram.send(format_event(e)):
                print(f"artel: telegram notification failed: {self.telegram.last_error}", file=sys.stderr)
            if self.command is not None and not self.command.send_event(e):
                print(f"artel: notify command failed for {e.kind}", file=sys.stderr)
        except Exception as exc:   # noqa: BLE001 - a notifier must never stop the runner
            token = self.telegram.creds.token if self.telegram else ""
            print(f"artel: notification error: {_scrub(str(exc), [token])}", file=sys.stderr)


# install-time check --------------------------------------------------------------------------------------------

HOW_TO_CONNECT = (
    "Telegram: no bot token and chat id found. To connect one, set ARTEL_TELEGRAM_TOKEN and ARTEL_TELEGRAM_CHAT\n"
    "(or TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID), or name files with `artel config set notify.telegram_token_file PATH`\n"
    "and `notify.telegram_chat_file PATH`, then run `artel notify test`. artel only sends; it never reads the bot's updates.")


def check_and_enable(repo: Repo, cfg, env: Mapping[str, str], out: TextIO, *, enable: bool = True) -> bool:
    creds = find_telegram_credentials(cfg, env)
    if creds is None:
        print(HOW_TO_CONNECT, file=out)
        return False
    notifier = TelegramNotifier(creds, cfg["notify.telegram_api_base"])
    username = notifier.get_me()
    if username is None:
        print(f"Telegram: getMe failed ({notifier.last_error}); the notifier stays disabled.", file=out)
        return False
    if not notifier.send(f"artel: notifier test from {Path(repo.root).name}"):
        print(f"Telegram: @{username} did not accept the test message ({notifier.last_error}); "
              "the notifier stays disabled.", file=out)
        return False
    print(f"Telegram: @{username}", file=out)
    if enable:
        config_mod.set_value(cfg.path, "notify.telegram_enabled", "true")
        print("notify.telegram_enabled = true", file=out)
    return True


def run_command(args) -> int:
    """`artel notify test`."""
    repo = Repo.discover(Path.cwd())
    cfg = config_mod.load_config(repo)
    env = os.environ
    have_creds = find_telegram_credentials(cfg, env) is not None
    telegram_ok = check_and_enable(repo, cfg, env, sys.stdout, enable=True)
    failed = have_creds and not telegram_ok
    if cfg["notify.command"]:
        ok = CommandNotifier(cfg["notify.command"], cfg["shell"]).send_event(
            Event("runner_stopped", text="notifier test"))
        print("Command: " + ("ok" if ok else "failed"))
        failed = failed or not ok
    return 1 if failed else 0
