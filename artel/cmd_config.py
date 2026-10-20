"""`artel config`, `artel config explain KEY`, `artel config set KEY VALUE` (spec §10)."""
from pathlib import Path

from artel import config as C, templates
from artel.config_schema import SETTINGS, by_key, toml_literal
from artel.errors import UsageError
from artel.gitutil import Repo


def _line(cfg: C.Config, key: str) -> str:
    setting = by_key(key)
    return f"{key} = {toml_literal(cfg.raw[key])}  ({cfg.source(key)})  — {setting.meaning}"


def run(args) -> int:
    repo = Repo.discover(Path.cwd())
    command = getattr(args, "config_cmd", None)
    key = getattr(args, "key", None)
    if command is None:
        cfg = C.load_config(repo)
        for setting in SETTINGS:
            print(_line(cfg, setting.key))
        for name in (*templates.TEMPLATE_NAMES, templates.NOTICE_NAME):
            print(f"template {name}: {templates.template_source(repo, cfg, name)}")
        return 0
    if key is None:
        raise UsageError(f"artel config {command}: a setting key is required")
    setting = by_key(key)
    if command == "explain":
        cfg = C.load_config(repo)
        print(key)
        print(f"default: {toml_literal(setting.default)}")
        print(f"value:   {toml_literal(cfg.raw[key])}  ({cfg.source(key)})")
        print()
        print(setting.explain)
        if setting.cost:
            print()
            print(f"cost: {setting.cost}")
        return 0
    value = getattr(args, "value", None)
    if value is None:
        raise UsageError("artel config set: a value is required")
    C.set_value(repo.root / "artel.toml", key, value)
    print(_line(C.load_config(repo), key))
    return 0
