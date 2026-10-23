"""Server configuration: `%MH_MCP_SERVER_HOME%/config.toml` laid over the built-in defaults (plan 051, Phase 2)."""

from __future__ import annotations

import tomllib
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

HOME_VARIABLE = "MH_MCP_SERVER_HOME"
CONFIG_FILE = "config.toml"

# The defaults, verbatim from the plan. This is also the file Phase 10 writes into the home.
DEFAULT_CONFIG_TOML = """\
[shell]
executable = "pwsh"
args = ["-NoLogo", "-NoProfile", "-NonInteractive", "-File"]

[execution]
defaultForegroundSeconds = 55
maxForegroundSeconds = 58
defaultCwd = "C:/sandbox/_work"

[output]
maxLines = 150
maxBytes = 24576

[read]
maxLines = 400
maxBytes = 49152

[paths]
writeRoots = ["C:/sandbox/_work"]
readRoots = ["C:/sandbox/_work", "C:/_mh/mhbp_home-PostgreSQL-disk-storage"]

[path]
prepend = []

[env]
PYTHONUTF8 = "1"

[secrets]
names = []

[excluded]
paths = []
"""

# table -> key -> kind. A table or key absent here is unknown, and unknown is an error: a misspelt key must never
# fall back to its default silently.
_SCHEMA: dict[str, dict[str, str]] = {
    "shell": {"executable": "str", "args": "str_list"},
    "execution": {"defaultForegroundSeconds": "int", "maxForegroundSeconds": "int", "defaultCwd": "str"},
    "output": {"maxLines": "int", "maxBytes": "int"},
    "read": {"maxLines": "int", "maxBytes": "int"},
    "paths": {"writeRoots": "str_list", "readRoots": "str_list"},
    "path": {"prepend": "str_list"},
    "secrets": {"names": "str_list"},
    "excluded": {"paths": "str_list"},
}
# Tables whose keys are free (environment-variable names); their values must be strings.
_FREE_TABLES = frozenset({"env"})

_KIND_TEXT = {"str": "a string", "int": "an integer >= 1", "str_list": "an array of strings"}


@dataclass(frozen=True)
class Config:
    shell_executable: str
    shell_args: tuple[str, ...]
    default_foreground_seconds: int
    max_foreground_seconds: int
    default_cwd: Path
    max_lines: int
    max_bytes: int
    read_max_lines: int
    read_max_bytes: int
    write_roots: tuple[Path, ...]
    read_roots: tuple[Path, ...]
    env: Mapping[str, str]
    path_prepend: tuple[str, ...]
    secret_names: frozenset[str]
    excluded_paths: tuple[str, ...]   # starts of absolute paths the file tools refuse


class ConfigError(Exception):
    """config.toml cannot be used: unparsable, an unknown table or key, a wrong type or an inconsistent value."""


def resolve_home(process_env: Mapping[str, str],
                 read_user: Callable[[str], str | None],
                 read_machine: Callable[[str], str | None]) -> Path | None:
    """`MH_MCP_SERVER_HOME` from the process environment, then the user's, then the machine's; None when absent.

    The registry readers are only called when the earlier source has no value. An empty value counts as absent.
    """
    sources: tuple[Callable[[], str | None], ...] = (
        lambda: _lookup_casefold(process_env, HOME_VARIABLE),
        lambda: read_user(HOME_VARIABLE),
        lambda: read_machine(HOME_VARIABLE),
    )
    for source in sources:
        value = source()
        if value:
            return Path(value)
    return None


def read_text_if_exists(path: Path) -> str | None:
    """The production `read_text` of `load_config`: the file's UTF-8 text, None when there is no such file."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None


def load_config(home: Path, read_text: Callable[[Path], str | None]) -> Config:
    """The configuration of `home`: its `config.toml` over the defaults; the defaults alone when the file is absent."""
    path = home / CONFIG_FILE
    merged = tomllib.loads(DEFAULT_CONFIG_TOML)
    text = read_text(path)
    if text is not None:
        try:
            user = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: not valid TOML: {e}") from e
        _overlay(merged, user, path)
    return _to_config(merged)


def is_under(path: Path, roots: Sequence[Path]) -> bool:
    """Whether `path`, once resolved, lies at or below one of `roots` — by path components, ignoring case.

    By components, so `C:/sandbox/_work2` is not under `C:/sandbox/_work`; resolved first, so
    `C:/sandbox/_work/../x` is not under it either.
    """
    parts = _resolved_parts(path)
    for root in roots:
        root_parts = _resolved_parts(root)
        if parts[:len(root_parts)] == root_parts:
            return True
    return False


def excluded_by(path: Path, excluded: Sequence[str]) -> str | None:
    """The first item of `excluded` that is a start of `path`, else None - a plain string prefix, ignoring case and
    treating / and \\ alike (so `C:\\Windows` also covers `C:\\WindowsApps`; an item ending in a separator covers one
    directory only)."""
    text = _separators_and_case(str(path))
    for item in excluded:
        if item and text.startswith(_separators_and_case(item)):
            return item
    return None


def _separators_and_case(text: str) -> str:
    return text.replace("/", "\\").casefold()


def _resolved_parts(path: Path) -> tuple[str, ...]:
    return tuple(part.casefold() for part in Path(path).resolve().parts)


def _lookup_casefold(env: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in env.items():
        if key.casefold() == wanted:
            return value
    return None


def _overlay(merged: dict[str, Any], user: Mapping[str, Any], path: Path) -> None:
    for table, values in user.items():
        if table not in _SCHEMA and table not in _FREE_TABLES:
            raise ConfigError(f"{path}: unknown table or key '{table}'")
        if not isinstance(values, dict):
            raise ConfigError(f"{path}: '{table}' must be a table")
        for key, value in values.items():
            name = f"{table}.{key}"
            if table in _FREE_TABLES:
                if not isinstance(value, str):
                    raise ConfigError(f"{path}: '{name}' must be a string, got {value!r}")
            else:
                kind = _SCHEMA[table].get(key)
                if kind is None:
                    raise ConfigError(f"{path}: unknown key '{name}'")
                if not _is_kind(kind, value):
                    raise ConfigError(f"{path}: '{name}' must be {_KIND_TEXT[kind]}, got {value!r}")
            merged[table][key] = value


def _is_kind(kind: str, value: Any) -> bool:
    if kind == "str":
        return isinstance(value, str)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1
    if kind == "str_list":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    raise AssertionError(f"unknown kind {kind}")


def _to_config(m: Mapping[str, Any]) -> Config:
    execution = m["execution"]
    if execution["maxForegroundSeconds"] < execution["defaultForegroundSeconds"]:
        raise ConfigError(f"execution.maxForegroundSeconds ({execution['maxForegroundSeconds']}) is less than "
                          f"execution.defaultForegroundSeconds ({execution['defaultForegroundSeconds']})")
    return Config(
        shell_executable=m["shell"]["executable"],
        shell_args=tuple(m["shell"]["args"]),
        default_foreground_seconds=execution["defaultForegroundSeconds"],
        max_foreground_seconds=execution["maxForegroundSeconds"],
        default_cwd=Path(execution["defaultCwd"]),
        max_lines=m["output"]["maxLines"],
        max_bytes=m["output"]["maxBytes"],
        read_max_lines=m["read"]["maxLines"],
        read_max_bytes=m["read"]["maxBytes"],
        write_roots=tuple(Path(p) for p in m["paths"]["writeRoots"]),
        read_roots=tuple(Path(p) for p in m["paths"]["readRoots"]),
        env=types.MappingProxyType(dict(m["env"])),
        path_prepend=tuple(m["path"]["prepend"]),
        secret_names=frozenset(m["secrets"]["names"]),
        excluded_paths=tuple(m["excluded"]["paths"]),
    )
