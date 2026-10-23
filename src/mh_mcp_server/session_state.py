"""Session state on disk: the working directory, the environment delta and the foreground lock (Phase 6).

`state/<sessionId>.json` holds `{"cwd", "envSet", "envRemoved"}`; `state/<sessionId>.fg.lock` holds the call that has
the session's foreground. A lock is stale when its call has `exit.json`, its supervisor is dead, or its server is dead
(the call is then a job); a stale lock is taken over.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from mh_mcp_server.calls import runs_dir
from mh_mcp_server.config import is_under
from mh_mcp_server.processes import process_alive, process_created

STATE_DIR = "state"
_LOCK_RETRY_SECONDS = 2.0


@dataclass(frozen=True)
class SessionState:
    cwd: Path
    env_set: Mapping[str, str] = field(default_factory=lambda: types.MappingProxyType({}))
    env_removed: frozenset[str] = frozenset()


def state_path(home: Path, session_id: str) -> Path:
    return home / STATE_DIR / f"{session_id}.json"


def lock_path(home: Path, session_id: str) -> Path:
    return home / STATE_DIR / f"{session_id}.fg.lock"


def state_exists(home: Path, session_id: str) -> bool:
    return state_path(home, session_id).is_file()


def load_state(home: Path, session_id: str, default_cwd: Path) -> SessionState:
    """The persisted state, or a fresh one at `default_cwd` when the session has none yet."""
    record = _read_json(state_path(home, session_id))
    if record is None:
        return SessionState(cwd=default_cwd)
    return SessionState(cwd=Path(record["cwd"]),
                        env_set=types.MappingProxyType(dict(record.get("envSet", {}))),
                        env_removed=frozenset(record.get("envRemoved", [])))


def save_state(home: Path, session_id: str, state: SessionState) -> None:
    _write_json_atomic(state_path(home, session_id), {
        "cwd": str(state.cwd),
        "envSet": dict(state.env_set),
        "envRemoved": sorted(state.env_removed),
    })


def resolve_cwd(cwd: Path, default_cwd: Path, is_dir: Callable[[Path], bool] = Path.is_dir) -> tuple[Path, list[str]]:
    """The directory a call starts in: `cwd`, or `default_cwd` with the note `cwdReset` when `cwd` is gone."""
    if is_dir(cwd):
        return cwd, []
    return default_cwd, ["cwdReset"]


def apply_state_out(state: SessionState, state_out: Mapping, write_roots: Sequence[Path],
                    not_persisted: frozenset[str]) -> tuple[SessionState, list[str]]:
    """The session state after a foreground call reported `state_out`, and its notes.

    The new working directory is kept only under `write_roots` (otherwise the note `cwdNotPersisted`). A name in
    `not_persisted` (compared case-insensitively) is never persisted, set or removed.
    """
    notes: list[str] = []
    blocked = {name.casefold() for name in not_persisted}
    cwd = state.cwd
    reported = state_out.get("cwd")
    if reported:
        if is_under(Path(reported), write_roots):
            cwd = Path(reported)
        else:
            notes.append("cwdNotPersisted")
    env_set = {name.casefold(): (name, value) for name, value in state.env_set.items()}
    env_removed = {name.casefold(): name for name in state.env_removed}
    for name, value in (state_out.get("envSet") or {}).items():
        if name.casefold() in blocked:
            continue
        env_set[name.casefold()] = (name, value)
        env_removed.pop(name.casefold(), None)
    for name in state_out.get("envRemoved") or []:
        if name.casefold() in blocked:
            continue
        env_set.pop(name.casefold(), None)
        env_removed[name.casefold()] = name
    return SessionState(cwd=cwd,
                        env_set=types.MappingProxyType({n: v for n, v in env_set.values()}),
                        env_removed=frozenset(env_removed.values())), notes


def acquire_foreground(home: Path, session_id: str, call_id: str, supervisor: tuple[int, int] | None,
                       server: tuple[int, int], alive: Callable[[int, int], bool]) -> str | None:
    """Takes the session's foreground for `call_id`: None when acquired, else the holder's callId.

    `supervisor` is None when the lock is taken before the supervisor is spawned; `update_foreground` records it.
    """
    lock = lock_path(home, session_id)
    lock.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(_lock_record(call_id, supervisor, server)).encode("utf-8")
    deadline = time.monotonic() + _LOCK_RETRY_SECONDS
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            holder = _read_json(lock)
            if holder is None:
                # being written or taken over at this moment
                if time.monotonic() > deadline:
                    return "unknown"
                time.sleep(0.005)
                continue
            if not _is_stale(home, session_id, holder, alive):
                return holder["callId"]
            if time.monotonic() > deadline:
                return holder["callId"]
            _remove_if_unchanged(lock, holder, alive)
            continue
        except PermissionError:
            # the previous lock is being deleted (delete-pending on Windows) - the name frees in a moment
            if time.monotonic() > deadline:
                return "unknown"
            time.sleep(0.005)
            continue
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return None


def update_foreground(home: Path, session_id: str, call_id: str, supervisor: tuple[int, int],
                      server: tuple[int, int]) -> None:
    """Records the supervisor of the call holding the foreground lock."""
    lock = lock_path(home, session_id)
    holder = _read_json(lock)
    if holder is None or holder.get("callId") != call_id:
        return
    _write_json_atomic(lock, _lock_record(call_id, supervisor, server))


def release_foreground(home: Path, session_id: str, call_id: str) -> None:
    """Releases the foreground lock when `call_id` holds it; a lock held by another call is left alone."""
    lock = lock_path(home, session_id)
    holder = _read_json(lock)
    if holder is not None and holder.get("callId") == call_id:
        _remove_if_unchanged(lock, holder)


def foreground_holder(home: Path, session_id: str, alive: Callable[[int, int], bool]) -> str | None:
    """The callId holding the foreground, None when there is no lock or it is stale."""
    holder = _read_json(lock_path(home, session_id))
    if holder is None or _is_stale(home, session_id, holder, alive):
        return None
    return holder["callId"]


def _lock_record(call_id: str, supervisor: tuple[int, int] | None, server: tuple[int, int]) -> dict:
    return {
        "callId": call_id,
        "supervisorPid": None if supervisor is None else supervisor[0],
        "supervisorCreated": None if supervisor is None else supervisor[1],
        "serverPid": server[0],
        "serverCreated": server[1],
    }


def _is_stale(home: Path, session_id: str, holder: Mapping, alive: Callable[[int, int], bool]) -> bool:
    if (runs_dir(home, session_id) / holder["callId"] / "exit.json").is_file():
        return True
    if holder.get("supervisorPid") is not None and not alive(holder["supervisorPid"], holder["supervisorCreated"]):
        return True
    return not alive(holder["serverPid"], holder["serverCreated"])


def _remove_if_unchanged(lock: Path, expected: Mapping, alive: Callable[[int, int], bool] = process_alive) -> None:
    """Removes `lock` when it still holds `expected`.

    Every removal of a lock - takeover and release - runs under the `<lock>.mutex` file (O_CREAT | O_EXCL), and
    re-reads the lock under it. A lock is only ever created by O_EXCL, so between that re-read and the removal
    nobody else can remove it or put a new one in its place. A mutex whose process is dead is taken over.
    """
    mutex = lock.with_name(lock.name + ".mutex")
    deadline = time.monotonic() + _LOCK_RETRY_SECONDS
    while True:
        try:
            fd = os.open(mutex, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
            break
        except FileExistsError:
            owner = _read_json(mutex)
            if owner is not None and not alive(owner["pid"], owner["created"]):
                _remove_quietly(mutex)
        except PermissionError:
            pass  # delete-pending
        if time.monotonic() > deadline:
            return  # the caller decides on its own deadline
        time.sleep(0.002)
    try:
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "created": process_created(os.getpid())}).encode("utf-8"))
        finally:
            os.close(fd)
        for _ in range(200):
            if _read_json(lock) != dict(expected):
                return
            try:
                os.remove(lock)
                return
            except FileNotFoundError:
                return
            except PermissionError:
                time.sleep(0.005)  # open for reading in another process or thread right now
    finally:
        _remove_quietly(mutex)


def _remove_quietly(path: Path) -> None:
    for _ in range(200):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.005)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.1)
