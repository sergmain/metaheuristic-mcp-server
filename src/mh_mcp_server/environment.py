"""The environment a command runs with, rebuilt for every call (plan 051, decision 12, Phase 3).

Order, a later layer winning: (a) the registry - machine, then user, PATH = machine PATH + ';' + user PATH;
(b) the per-process variables in VOLATILE copied from the server's own environment; (c) the fixed variables
(MH_MCP_SESSION_ID, MH_MCP_CALL_ID); (d) config [env], then config [path] prepend in front of PATH; (e) the
session's delta - set, then removed. Nothing else of the server's environment is passed on.

Windows environment names are case-insensitive: names are compared by casefold(), and a name keeps the casing of
the layer that set it last.
"""

from __future__ import annotations

import os
from typing import Iterable, Literal, Mapping, Sequence

# decision 12 (b), verbatim - then the per-logon variables Windows sets in every logon session's environment and
# keeps out of the registry, found missing by plan 051 Phase 10 probe 2 (the JB terminal has them)
VOLATILE: tuple[str, ...] = (
    "SYSTEMROOT", "WINDIR", "COMPUTERNAME", "USERNAME", "USERDOMAIN", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "COMMONPROGRAMFILES", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    "SYSTEMDRIVE", "ALLUSERSPROFILE", "PUBLIC", "PROGRAMW6432", "COMMONPROGRAMW6432", "COMMONPROGRAMFILES(X86)",
    "LOGONSERVER", "SESSIONNAME", "USERDOMAIN_ROAMINGPROFILE",
)
_VOLATILE_FOLDED = frozenset(name.casefold() for name in VOLATILE)

_REGISTRY_KEYS = {
    "machine": ("HKEY_LOCAL_MACHINE", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    "user": ("HKEY_CURRENT_USER", "Environment"),
}

_PATH = "path"


def expand(value: str, lookup: Mapping[str, str]) -> str:
    """`%NAME%` replaced by NAME's value in `lookup` (case-insensitive); an unknown reference is kept verbatim.

    When the text between two '%' is not a known name, the first '%' is kept and the scan continues at the second,
    so `100% and %X%` still expands `%X%`.
    """
    folded = {k.casefold(): v for k, v in lookup.items()}
    out: list[str] = []
    i = 0
    while i < len(value):
        start = value.find("%", i)
        if start < 0:
            out.append(value[i:])
            break
        out.append(value[i:start])
        end = value.find("%", start + 1)
        if end < 0:
            out.append(value[start:])
            break
        name = value[start + 1:end]
        if name and name.casefold() in folded:
            out.append(folded[name.casefold()])
            i = end + 1
        else:
            out.append("%")
            i = start + 1
    return "".join(out)


def read_registry_env(hive: Literal["machine", "user"], base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The variables of one registry hive, `REG_EXPAND_SZ` values expanded.

    Expansion is against `base` (default: this process's environment) overlaid with the hive's own values - two
    passes, so an expandable value may refer to another one of the same hive. Pass the machine values in `base` when
    reading "user", as Windows builds the user environment on top of the machine one. Other value kinds are skipped.
    """
    import winreg

    root_name, sub_key = _REGISTRY_KEYS[hive]
    raw_sz: dict[str, str] = {}
    raw_expand: dict[str, str] = {}
    try:
        key = winreg.OpenKey(getattr(winreg, root_name), sub_key)
    except FileNotFoundError:
        return {}
    with key:
        index = 0
        while True:
            try:
                name, value, kind = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            if kind == winreg.REG_SZ:
                raw_sz[name] = value
            elif kind == winreg.REG_EXPAND_SZ:
                raw_expand[name] = value
    return expand_hive(raw_sz, raw_expand, dict(os.environ) if base is None else base)


def expand_hive(raw_sz: Mapping[str, str], raw_expand: Mapping[str, str], base: Mapping[str, str]) -> dict[str, str]:
    """The pure part of `read_registry_env`: one hive's REG_SZ values kept, its REG_EXPAND_SZ values expanded."""
    known = {**base, **raw_sz}
    first = {name: expand(value, known) for name, value in raw_expand.items()}
    second = {name: expand(value, {**known, **first}) for name, value in raw_expand.items()}
    return {**raw_sz, **second}


def compose_env(machine: Mapping[str, str], user: Mapping[str, str], server_env: Mapping[str, str],
                fixed: Mapping[str, str], config_env: Mapping[str, str], path_prepend: Sequence[str],
                delta_set: Mapping[str, str], delta_removed: Iterable[str]) -> dict[str, str]:
    """The command environment of decision 12; see the module docstring for the order."""
    env = _Env()
    env.update(machine)
    machine_path = _get(machine, _PATH)
    env.update(user)
    user_path = _get(user, _PATH)
    if machine_path is not None and user_path is not None:
        env.set(_name_of(user, _PATH), _join_path([machine_path, user_path]))
    env.update({k: v for k, v in server_env.items() if k.casefold() in _VOLATILE_FOLDED})
    env.update(fixed)
    env.update(config_env)
    if path_prepend:
        current = env.get(_PATH)
        env.set(env.name_of(_PATH) or "PATH", _join_path([*path_prepend, *([current] if current else [])]))
    env.update(delta_set)
    for name in delta_removed:
        env.remove(name)
    return env.as_dict()


def mask(text: str, secrets: Mapping[str, str]) -> str:
    """`text` with every occurrence of every non-empty secret value replaced by `***`, longest value first."""
    for value in sorted({v for v in secrets.values() if v}, key=len, reverse=True):
        text = text.replace(value, "***")
    return text


def _join_path(parts: Sequence[str]) -> str:
    return ";".join(p.strip(";") for p in parts if p.strip(";"))


def _get(mapping: Mapping[str, str], folded_name: str) -> str | None:
    for k, v in mapping.items():
        if k.casefold() == folded_name:
            return v
    return None


def _name_of(mapping: Mapping[str, str], folded_name: str) -> str:
    for k in mapping:
        if k.casefold() == folded_name:
            return k
    return folded_name.upper()


class _Env:
    """A case-insensitive environment that keeps the casing of the layer that set a name last."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, str]] = {}

    def set(self, name: str, value: str) -> None:
        self._items[name.casefold()] = (name, value)

    def update(self, values: Mapping[str, str]) -> None:
        for name, value in values.items():
            self.set(name, value)

    def get(self, folded_name: str) -> str | None:
        item = self._items.get(folded_name)
        return None if item is None else item[1]

    def name_of(self, folded_name: str) -> str | None:
        item = self._items.get(folded_name)
        return None if item is None else item[0]

    def remove(self, name: str) -> None:
        self._items.pop(name.casefold(), None)

    def as_dict(self) -> dict[str, str]:
        return {name: value for name, value in self._items.values()}
