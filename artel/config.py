"""Loading, validating and editing artel.toml (spec §9, §10)."""
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from artel.config_schema import SETTINGS, Setting, by_key, toml_literal
from artel.errors import UsageError
from artel.gitutil import Repo

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)\s*$")
_DURATION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}
_TABLES = ("rotation", "models", "commands", "profiles", "notify")


def parse_duration(s: str | int | float) -> float:
    if isinstance(s, bool):
        raise UsageError(f"invalid duration: {s!r}")
    if isinstance(s, (int, float)):
        if not math.isfinite(s) or s < 0:
            raise UsageError(f"invalid duration: {s!r}")
        return float(s)
    if isinstance(s, str):
        m = _DURATION.match(s)
        if m:
            unit = m.group(2)
            number = float(m.group(1))
            return number / 1000 if unit == "ms" else number * _DURATION_UNITS[unit]
        if re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", s):
            return float(s)
    raise UsageError(f"invalid duration: {s!r} (use e.g. 500ms, 30s, 90m, 2h, 1d)")


def parse_size(s: str | int) -> int:
    if isinstance(s, bool):
        raise UsageError(f"invalid size: {s!r}")
    if isinstance(s, int):
        if s < 0:
            raise UsageError(f"invalid size: {s!r}")
        return s
    if isinstance(s, str):
        m = _SIZE.match(s)
        if m:
            return int(float(m.group(1)) * _SIZE_UNITS[m.group(2).upper()])
    raise UsageError(f"invalid size: {s!r} (use e.g. 500M, 3G, 1024K or plain bytes)")


def _toml_array(text: str, key: str) -> list:
    try:
        value = tomllib.loads("v = " + text)["v"]
    except tomllib.TOMLDecodeError as e:
        raise UsageError(f"{key}: invalid list {text!r}: {e}") from None
    if not isinstance(value, list):
        raise UsageError(f"{key}: expected a list, got {text!r}")
    return value


def _str_items(items: list, key: str) -> list[str]:
    if not all(isinstance(i, str) for i in items):
        raise UsageError(f"{key}: every list item must be a string")
    return list(items)


def _list_from(raw: object, key: str) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return _str_items(list(raw), key)
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            return _str_items(_toml_array(text, key), key)
        return [part.strip() for part in text.split(",") if part.strip()]
    raise UsageError(f"{key}: expected a list of strings, got {raw!r}")


def _normalize(setting: Setting, raw: object) -> object:
    """Validate `raw` (a TOML value or a string from env / `config set`); return it TOML-typed."""
    key, kind = setting.key, setting.kind
    if kind == "int":
        if isinstance(raw, str):
            if not re.fullmatch(r"-?[0-9]+", raw.strip()):
                raise UsageError(f"{key}: expected an integer, got {raw!r}")
            raw = int(raw.strip())
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise UsageError(f"{key}: expected an integer, got {raw!r}")
        if setting.minimum is not None and raw < setting.minimum:
            raise UsageError(f"{key}: must be at least {setting.minimum}, got {raw}")
        return raw
    if kind == "str":
        if not isinstance(raw, str):
            raise UsageError(f"{key}: expected a string, got {raw!r}")
        return raw
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in _TRUE | _FALSE:
            return raw.strip().lower() in _TRUE
        raise UsageError(f"{key}: expected true or false, got {raw!r}")
    if kind in ("duration", "size"):
        try:
            (parse_duration if kind == "duration" else parse_size)(raw)
        except UsageError as e:
            raise UsageError(f"{key}: {e}") from None
        return raw.strip() if isinstance(raw, str) else raw
    if kind == "str_list":
        return _list_from(raw, key)
    if kind == "enum":
        if raw not in setting.choices:
            raise UsageError(f"{key}: must be one of {', '.join(setting.choices)}, got {raw!r}")
        return raw
    if kind == "enum_list":
        items = _list_from(raw, key)
        bad = [i for i in items if i not in setting.choices]
        if bad:
            raise UsageError(f"{key}: unknown value {bad[0]!r}; choose from {', '.join(setting.choices)}")
        return items
    if kind in ("command", "argv"):
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("["):
                raw = _str_items(_toml_array(text, key), key)
            else:
                if kind == "argv" and not text:
                    raise UsageError(f"{key}: must not be empty")
                return raw if kind == "argv" else text
        if isinstance(raw, (list, tuple)):
            items = _str_items(list(raw), key)
            if kind == "argv" and not items:
                raise UsageError(f"{key}: must not be empty")
            return items
        raise UsageError(f"{key}: expected a string or a list of strings, got {raw!r}")
    raise AssertionError(f"unknown setting kind {kind!r}")   # pragma: no cover


def _copy(value: object) -> object:
    return list(value) if isinstance(value, list) else value


def _runtime(setting: Setting, value: object) -> object:
    kind = setting.kind
    if kind == "duration":
        return parse_duration(value)
    if kind == "size":
        return parse_size(value)
    if kind == "command" and not value:
        return ""                       # "" and [] both mean unset
    if isinstance(value, list):
        return list(value)
    return value


def coerce(setting: Setting, raw: object) -> object:
    """The runtime value: durations → seconds, sizes → bytes, unset command → ""."""
    return _runtime(setting, _normalize(setting, raw))


@dataclass(frozen=True)
class Config:
    path: Path
    values: Mapping[str, object]
    sources: Mapping[str, str]
    raw: Mapping[str, object] = field(default_factory=dict)   # TOML-typed values, as shown by `artel config`

    def __getitem__(self, key: str) -> object:
        by_key(key)
        return self.values[key]

    def source(self, key: str) -> str:
        by_key(key)
        return self.sources[key]


def _flatten(data: Mapping[str, object], path: Path) -> dict[str, object]:
    flat: dict[str, object] = {}
    for name, value in data.items():
        if isinstance(value, dict) and name in _TABLES:
            for sub, sub_value in value.items():
                flat[f"{name}.{sub}"] = sub_value
        else:
            flat[name] = value
    for key in flat:
        try:
            by_key(key)
        except UsageError:
            raise UsageError(f"unknown setting {key!r} in {path}") from None
    return flat


def env_name(key: str) -> str:
    return "ARTEL_" + key.replace(".", "_").upper()


def load_config(repo: Repo, *, env: Mapping[str, str] | None = None,
                cli: Mapping[str, tuple[object, str]] | None = None) -> Config:
    path = repo.root / "artel.toml"
    env = os.environ if env is None else env
    raw: dict[str, object] = {s.key: _copy(s.default) for s in SETTINGS}
    sources = {s.key: "default" for s in SETTINGS}
    if path.is_file():
        try:
            data = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise UsageError(f"cannot parse {path}: {e}") from None
        for key, value in _flatten(data, path).items():
            raw[key] = _normalize_in(by_key(key), value, f"{path.name}")
            sources[key] = "artel.toml"
    for s in SETTINGS:
        name = env_name(s.key)
        if name in env:
            raw[s.key] = _normalize(s, env[name])
            sources[s.key] = f"env {name}"
    for key, (value, source) in (cli or {}).items():
        raw[key] = _normalize(by_key(key), value)
        sources[key] = source
    values = {s.key: _runtime(s, raw[s.key]) for s in SETTINGS}
    return Config(path=path, values=values, sources=sources, raw=raw)


def _normalize_in(setting: Setting, value: object, where: str) -> object:
    try:
        return _normalize(setting, value)
    except UsageError as e:
        raise UsageError(f"{e} (in {where})") from None


# artel.toml rendering and editing ------------------------------------------------

def _line(setting: Setting, comments: bool) -> str:
    name = setting.key.rpartition(".")[2]
    text = f"{name} = {toml_literal(setting.default)}"
    if comments:
        comment = setting.meaning + (f" — {setting.cost}" if setting.cost else "")
        text += f"  # {comment}"
    return text


def render_toml(*, comments: bool = True) -> str:
    lines = []
    if comments:
        lines += ["# artel settings. `artel config` shows every value and where it comes from;",
                  "# `artel config explain KEY` explains one.", ""]
    lines += [_line(s, comments) for s in SETTINGS if "." not in s.key]
    for table in _TABLES:
        lines += ["", f"[{table}]"]
        lines += [_line(s, comments) for s in SETTINGS if s.key.startswith(table + ".")]
    return "\n".join(lines) + "\n"


_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
_KEY_LINE = re.compile(r"^(\s*)((?:[A-Za-z0-9_-]+|\"[^\"]*\"|'[^']*')(?:\s*\.\s*(?:[A-Za-z0-9_-]+|\"[^\"]*\"|'[^']*'))*)\s*=\s*")


def _clean_key(text: str) -> str:
    parts = re.findall(r"[A-Za-z0-9_-]+|\"[^\"]*\"|'[^']*'", text)
    return ".".join(p.strip("\"'") for p in parts)


def _value_end(lines: list[str], idx: int, col: int) -> tuple[int, int]:
    """(line index, column) just past the TOML value that starts at lines[idx][col]; trailing space excluded."""
    depth = 0
    last = (idx, col)
    quote = ""
    i = idx
    while i < len(lines):
        line = lines[i].rstrip("\r\n")
        j = col if i == idx else 0
        while j < len(line):
            ch = line[j]
            if quote:
                if ch == "\\" and quote == '"':
                    j += 2
                    last = (i, min(j, len(line)))
                    continue
                if ch == quote:
                    quote = ""
                last = (i, j + 1)
            elif ch in "\"'":
                quote = ch
                last = (i, j + 1)
            elif ch == "#":
                break
            elif ch in "[{":
                depth += 1
                last = (i, j + 1)
            elif ch in "]}":
                depth -= 1
                last = (i, j + 1)
            elif not ch.isspace():
                last = (i, j + 1)
            j += 1
        if depth <= 0 and not quote:
            return last
        i += 1
    return last


def _edit(text: str, key: str, literal: str) -> str:
    lines = text.splitlines(keepends=True)
    eol = "\r\n" if "\r\n" in text else "\n"
    table, _, name = key.rpartition(".")
    current = ""
    section_last = None            # last non-blank line index inside the wanted table
    first_header = None
    table_seen = False
    skip_to = -1
    for i, line in enumerate(lines):
        if i <= skip_to:
            continue            # continuation line of a multi-line value
        header = _HEADER.match(line)
        if header:
            if first_header is None:
                first_header = i
            current = _clean_key(header.group(1))
            table_seen = table_seen or current == table
            continue
        km = _KEY_LINE.match(line)
        if km:
            full = _clean_key(km.group(2))
            if current:
                full = current + "." + full
            end_line, end_col = _value_end(lines, i, km.end())
            skip_to = end_line
            if full == key:
                tail = lines[end_line][end_col:]
                lines[i:end_line + 1] = [lines[i][:km.end()] + literal + tail]
                return "".join(lines)
        if current == table and line.strip() and not line.lstrip().startswith("#"):
            section_last = i
    new_line = f"{name} = {literal}{eol}"
    if table == "":
        at = first_header if first_header is not None else len(lines)
        if at == len(lines) and lines and not lines[-1].endswith("\n"):
            lines[-1] += eol
        lines.insert(at, new_line)
        return "".join(lines)
    if table_seen:
        at = (section_last if section_last is not None else _header_index(lines, table)) + 1
        if not lines[at - 1].endswith("\n"):
            lines[at - 1] += eol
        lines.insert(at, new_line)
        return "".join(lines)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += eol
    if lines and lines[-1].strip():
        lines.append(eol)
    lines += [f"[{table}]{eol}", new_line]
    return "".join(lines)


def _header_index(lines: list[str], table: str) -> int:
    for i, line in enumerate(lines):
        header = _HEADER.match(line)
        if header and _clean_key(header.group(1)) == table:
            return i
    raise AssertionError("table header vanished")   # pragma: no cover


def set_value(path: Path, key: str, raw: str) -> None:
    """Validate and write `key = raw` into artel.toml, keeping every other line and the key's comment."""
    setting = by_key(key)
    value = _normalize(setting, raw)
    literal = toml_literal(value)
    path = Path(path)
    bom = ""
    original = ""
    if path.is_file():
        original = path.read_bytes().decode("utf-8")
        if original.startswith("\ufeff"):
            bom, original = "\ufeff", original[1:]
    updated = _edit(original, key, literal)
    try:
        parsed = _flatten(tomllib.loads(updated), path)
    except tomllib.TOMLDecodeError as e:
        raise UsageError(f"cannot set {key}: {path.name} would not parse ({e}); file left unchanged") from None
    if parsed.get(key) != value:
        raise UsageError(f"cannot set {key}: {path.name} would not read back the new value; file left unchanged")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(bom + updated)
