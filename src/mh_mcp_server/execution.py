"""The tool logic: sessions, the call lifecycle and executeCommand (plan 051, Phase 7).

Every collaborator - clock, random hex, registry reader, spawner, liveness - comes in through `Runtime`, so the logic
runs unchanged against real processes in the tests. Blocking work runs in worker threads; waiting for a command is an
async poll for `exit.json`.

A job is a call whose result was never delivered in the foreground: `startJob`, a foreground call promoted when its
wait ran out, or a foreground call whose server died while waiting. A delivered foreground call has
`delivered.json`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import anyio
import anyio.to_thread

import mh_mcp_server
from mh_mcp_server import __version__
from mh_mcp_server.calls import (allocate_call, call_state, read_call, read_exit, runs_dir, supervisor_argv,
                                 write_call)
from mh_mcp_server.config import Config, excluded_by, excluded_in_text, is_under
from mh_mcp_server.environment import VOLATILE, compose_env, read_registry_env
from mh_mcp_server.output import read_head_tail
from mh_mcp_server.processes import process_alive, process_created, spawn_detached
from mh_mcp_server.session_id import is_valid_session_id, new_session_id, random_hex6
from mh_mcp_server.session_state import (SessionState, acquire_foreground, apply_state_out, foreground_holder,
                                         load_state, release_foreground, resolve_cwd, save_state, state_exists,
                                         update_foreground)
from mh_mcp_server.transcript import transcript_path
from mh_mcp_server.wrapper import render_wrapper

FIXED_NAMES = frozenset({"MH_MCP_SESSION_ID", "MH_MCP_CALL_ID"})
SERVER_ROOT = Path(mh_mcp_server.__file__).resolve().parent.parent
_VOLATILE_FOLDED = frozenset(name.casefold() for name in VOLATILE)


@dataclass(frozen=True)
class Reply:
    """What a tool returns: the JSON header (content 0), the raw text (content 1) and whether it is a refusal."""
    header: dict
    text: str | None = None
    is_error: bool = False
    call_id: str | None = None        # the transcript block's call, when there is one
    session_id: str | None = None     # the session whose transcript gets the block


def refusal(kind: str, session_id: str | None = None, **fields: Any) -> Reply:
    return Reply({"error": kind, **fields}, None, True, None, session_id)


@dataclass(frozen=True)
class Runtime:
    home: Path
    config: Config
    server: tuple[int, int]                                         # this server's pid and creation time
    server_env: Mapping[str, str]
    read_registry: Callable[[str, Mapping[str, str]], dict[str, str]]
    spawn: Callable[[Sequence[str], Mapping[str, str], Path], int]
    alive: Callable[[int, int | None], bool]
    created: Callable[[int], int | None]
    now: Callable[[], datetime]
    hex6: Callable[[], str]
    poll_seconds: float = 0.25


def production_runtime(home: Path, config: Config) -> Runtime:
    return Runtime(home=home, config=config, server=(os.getpid(), process_created(os.getpid())),
                   server_env=dict(os.environ), read_registry=read_registry_env, spawn=spawn_detached,
                   alive=process_alive, created=process_created, now=lambda: datetime.now().astimezone(),
                   hex6=random_hex6)


def not_persisted(config: Config) -> frozenset[str]:
    return FIXED_NAMES | config.secret_names


def secret_values(rt: Runtime) -> dict[str, str]:
    """The values to mask: config [env] and the registry, for the names in config [secrets]; plus their JSON-escaped
    forms, so a value is masked inside a JSON header too."""
    if not rt.config.secret_names:
        return {}
    wanted = {name.casefold() for name in rt.config.secret_names}
    volatile = _volatile(rt.server_env)
    machine = rt.read_registry("machine", volatile)
    user = rt.read_registry("user", {**volatile, **machine})
    values: dict[str, str] = {}
    for layer in (machine, user, rt.config.env):
        for name, value in layer.items():
            if name.casefold() in wanted and value:
                values[f"{name}:{len(values)}"] = value
                values[f"{name}:{len(values)}:json"] = json.dumps(value, ensure_ascii=False)[1:-1]
    return values


def command_env(rt: Runtime, session_id: str, call_id: str, state: SessionState) -> dict[str, str]:
    """The environment of one call, rebuilt from the registry (decision 12)."""
    volatile = _volatile(rt.server_env)
    machine = rt.read_registry("machine", volatile)
    user = rt.read_registry("user", {**volatile, **machine})
    return compose_env(machine, user, rt.server_env, {"MH_MCP_SESSION_ID": session_id, "MH_MCP_CALL_ID": call_id},
                       rt.config.env, rt.config.path_prepend, state.env_set, state.env_removed)


def spawn_call(rt: Runtime, session_id: str, call_id: str, call_dir: Path, command: str, cwd: Path,
               state: SessionState, kind: str, name: str | None) -> int:
    """Writes the call's files and starts its supervisor; the supervisor's pid."""
    (call_dir / "command.ps1").write_bytes(render_wrapper(command, call_dir / "state-out.json").encode("utf-8"))
    argv = [rt.config.shell_executable, *rt.config.shell_args, str(call_dir / "command.ps1")]
    write_call(call_dir, argv, cwd, rt.now(), {"kind": kind, "name": name})
    return rt.spawn(supervisor_argv(call_dir), command_env(rt, session_id, call_id, state), cwd)


def resolve_call_cwd(rt: Runtime, state: SessionState, cwd: str | None) -> tuple[Path, list[str]] | Reply:
    """The directory a call runs in: the given `cwd` (relative to the session's) or the session's own."""
    if cwd is None:
        return resolve_cwd(state.cwd, rt.config.default_cwd)
    path = (state.cwd / cwd).resolve()
    if not is_under(path, rt.config.write_roots):
        return refusal("CWD_OUTSIDE_ROOTS", cwd=str(path))
    if not path.is_dir():
        return refusal("CWD_NOT_FOUND", cwd=str(path))
    return path, []


def admit_call(rt: Runtime, state: SessionState, command: str, cwd: str | None) -> tuple[Path, list[str]] | Reply:
    """The directory a command runs in, once the command is admitted: refused when its text names an [excluded] path
    (`excluded_in_text` - a path written out, not one built at run time) or when the directory - the given `cwd` or the
    session's own - lies in one; otherwise as `resolve_call_cwd` decides."""
    named = excluded_in_text(command, rt.config.excluded_paths)
    if named is not None:
        return refusal("PATH_EXCLUDED", where="command", excluded=named)
    resolved = resolve_call_cwd(rt, state, cwd)
    if isinstance(resolved, Reply):
        return resolved
    excluded = excluded_by(resolved[0], rt.config.excluded_paths)
    if excluded is not None:
        return refusal("PATH_EXCLUDED", where="cwd", cwd=str(resolved[0]), excluded=excluded)
    return resolved


# ==================== getSessionId / getSession ====================

def get_session_id(rt: Runtime) -> Reply:
    session_id = new_session_id(rt.now, rt.hex6)
    save_state(rt.home, session_id, SessionState(cwd=rt.config.default_cwd))
    return Reply({"sessionId": session_id, "transcript": str(transcript_path(rt.home, session_id)),
                  "serverVersion": __version__, "serverRoot": str(SERVER_ROOT)}, session_id=session_id)


def get_session(rt: Runtime, session_id: str) -> Reply:
    if not is_valid_session_id(session_id):
        return refusal("BAD_SESSION_ID")
    if not state_exists(rt.home, session_id):
        save_state(rt.home, session_id, SessionState(cwd=rt.config.default_cwd))
    state = load_state(rt.home, session_id, rt.config.default_cwd)
    foreground = foreground_holder(rt.home, session_id, rt.alive)
    return Reply({"sessionId": session_id, "cwd": str(state.cwd), "envSet": sorted(state.env_set),
                  "envRemoved": sorted(state.env_removed), "foreground": foreground,
                  "jobs": list_jobs(rt, session_id, foreground)}, session_id=session_id)


def list_jobs(rt: Runtime, session_id: str, foreground: str | None) -> list[dict]:
    directory = runs_dir(rt.home, session_id)
    if not directory.is_dir():
        return []
    jobs = []
    for call_dir in sorted((p for p in directory.iterdir() if p.name.isdigit()), key=lambda p: int(p.name)):
        call = read_call(call_dir)
        if call is None or call_dir.name == foreground or (call_dir / "delivered.json").exists():
            continue
        exit_record = read_exit(call_dir)
        jobs.append({"jobId": call_dir.name, "name": call.get("name"), "state": call_state(call_dir, rt.alive),
                     "exitCode": None if exit_record is None else exit_record["exit"],
                     "startedAt": call["createdAt"], "logPath": str(call_dir / "output.log")})
    return jobs


# ==================== executeCommand ====================

@dataclass
class _Foreground:
    session_id: str
    call_id: str
    call_dir: Path
    cwd: Path
    cwd_given: bool
    state: SessionState
    notes: list[str] = field(default_factory=list)
    started: float = 0.0


async def execute_command(rt: Runtime, session_id: str, command: str, cwd: str | None = None,
                          timeout_seconds: int | None = None, max_lines: int | None = None) -> Reply:
    if not is_valid_session_id(session_id):
        return refusal("BAD_SESSION_ID")
    timeout = rt.config.default_foreground_seconds if timeout_seconds is None else timeout_seconds
    if timeout > rt.config.max_foreground_seconds:
        return refusal("TIMEOUT_TOO_LARGE", session_id, max=rt.config.max_foreground_seconds)
    if timeout < 0 or (max_lines is not None and max_lines < 1):
        return refusal("BAD_ARGUMENTS", session_id, message="timeoutSeconds must be >= 0 and maxLines >= 1")
    lines_budget = rt.config.max_lines if max_lines is None else max_lines
    started = await anyio.to_thread.run_sync(_start_foreground, rt, session_id, command, cwd)
    if isinstance(started, Reply):
        return started
    try:
        deadline = started.started + timeout
        while True:
            if await anyio.to_thread.run_sync(read_exit, started.call_dir) is not None:
                return await anyio.to_thread.run_sync(_finish_done, rt, started, lines_budget)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return await anyio.to_thread.run_sync(_promote, rt, started, lines_budget)
            await anyio.sleep(min(rt.poll_seconds, remaining))
    except BaseException:
        # never leave the session locked by a call nobody waits on
        await anyio.to_thread.run_sync(release_foreground, rt.home, session_id, started.call_id)
        raise


def _start_foreground(rt: Runtime, session_id: str, command: str, cwd: str | None) -> _Foreground | Reply:
    state = load_state(rt.home, session_id, rt.config.default_cwd)
    resolved = admit_call(rt, state, command, cwd)
    if isinstance(resolved, Reply):
        return Reply(resolved.header, None, True, None, session_id)
    call_cwd, notes = resolved
    call_id, call_dir = allocate_call(runs_dir(rt.home, session_id))
    holder = acquire_foreground(rt.home, session_id, call_id, None, rt.server, rt.alive)
    if holder is not None:
        call_dir.rmdir()
        return refusal("SESSION_BUSY", session_id, runningCallId=holder)
    started = time.monotonic()
    try:
        pid = spawn_call(rt, session_id, call_id, call_dir, command, call_cwd, state, "foreground", None)
    except BaseException:
        release_foreground(rt.home, session_id, call_id)
        raise
    update_foreground(rt.home, session_id, call_id, (pid, rt.created(pid)), rt.server)
    return _Foreground(session_id, call_id, call_dir, call_cwd, cwd is not None, state, notes, started)


def _finish_done(rt: Runtime, fg: _Foreground, lines_budget: int) -> Reply:
    try:
        exit_record = read_exit(fg.call_dir) or {}
        state_out = _read_json(fg.call_dir / "state-out.json")
        notes = list(fg.notes)
        if state_out is None:
            notes.append("stateNotCaptured")
            state_out = {}
        elif fg.cwd_given and _same_path(state_out.get("cwd"), fg.cwd):
            # a cwd given for this call only is not persisted unless the command itself changed directory
            state_out = {k: v for k, v in state_out.items() if k != "cwd"}
        new_state, more = apply_state_out(fg.state, state_out, rt.config.write_roots, not_persisted(rt.config))
        notes += more
        save_state(rt.home, fg.session_id, new_state)
        (fg.call_dir / "delivered.json").write_text(json.dumps({"at": rt.now().isoformat(timespec="milliseconds")}),
                                                    encoding="utf-8")
    finally:
        release_foreground(rt.home, fg.session_id, fg.call_id)
    log = fg.call_dir / "output.log"
    captured = read_head_tail(log, lines_budget, rt.config.max_bytes)
    header: dict[str, Any] = {"callId": fg.call_id, "exitCode": exit_record.get("exit"),
                              "seconds": round(time.monotonic() - fg.started, 2), "cwd": str(new_state.cwd),
                              "lines": captured.total}
    if captured.omitted:
        header["omitted"] = captured.omitted
    header["logPath"] = str(log)
    if captured.encoding_errors:
        header["encodingErrors"] = captured.encoding_errors
    if notes:
        header["notes"] = notes
    return Reply(header, "\n".join(captured.lines) or None, False, fg.call_id, fg.session_id)


def _promote(rt: Runtime, fg: _Foreground, lines_budget: int) -> Reply:
    """The wait ran out: the command continues as a job; its cwd and environment changes are discarded."""
    release_foreground(rt.home, fg.session_id, fg.call_id)
    log = fg.call_dir / "output.log"
    captured = read_head_tail(log, lines_budget, rt.config.max_bytes)
    header: dict[str, Any] = {"callId": fg.call_id, "state": "running", "jobId": fg.call_id,
                              "seconds": round(time.monotonic() - fg.started, 2), "lines": captured.total}
    if captured.omitted:
        header["omitted"] = captured.omitted
    header["logPath"] = str(log)
    return Reply(header, "\n".join(captured.lines) or None, False, fg.call_id, fg.session_id)


def _volatile(server_env: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in server_env.items() if k.casefold() in _VOLATILE_FOLDED}


def _same_path(reported: str | None, path: Path) -> bool:
    return reported is not None and str(Path(reported)).casefold() == str(path).casefold()


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
