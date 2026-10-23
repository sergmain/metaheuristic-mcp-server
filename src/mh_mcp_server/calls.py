"""Call directories `runs/<sessionId>/<callId>/` and the state derived from their files (plan 051, § 3, Phase 5).

A call's state comes from files only: `exit.json` present -> done; absent and its supervisor alive -> running;
absent and its supervisor dead -> lost.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Sequence

RUNS_DIR = "runs"
SUPERVISOR = Path(__file__).resolve().parent / "supervisor.py"
# A supervisor writes pid.json a moment after it starts; until then a young call is running, not lost.
STARTUP_GRACE_SECONDS = 30.0

CallState = Literal["running", "done", "lost"]


def runs_dir(home: Path, session_id: str) -> Path:
    return home / RUNS_DIR / session_id


def allocate_call(runs_dir: Path) -> tuple[str, Path]:
    """The next free call id of `runs_dir` and its new directory: `mkdir` of the next NNNN, retried when taken."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    while True:
        number = 1 + max((int(p.name) for p in runs_dir.iterdir() if p.name.isdigit()), default=0)
        call_id = f"{number:04d}"
        call_dir = runs_dir / call_id
        try:
            call_dir.mkdir()
        except FileExistsError:
            continue
        return call_id, call_dir


def write_call(call_dir: Path, argv: Sequence[str], cwd: Path, created_at: datetime) -> None:
    """`call.json` - argv, cwd and the creation time; never the environment (decision 12)."""
    _write_json_atomic(call_dir / "call.json",
                       {"argv": list(argv), "cwd": str(cwd), "createdAt": created_at.isoformat(timespec="milliseconds")})


def supervisor_argv(call_dir: Path) -> list[str]:
    """How the supervisor of `call_dir` is started: by file path, with this interpreter."""
    return [sys.executable, str(SUPERVISOR), str(call_dir)]


def read_exit(call_dir: Path) -> dict | None:
    return _read_json(call_dir / "exit.json")


def read_pids(call_dir: Path) -> dict | None:
    return _read_json(call_dir / "pid.json")


def read_call(call_dir: Path) -> dict | None:
    return _read_json(call_dir / "call.json")


def call_state(call_dir: Path, alive: Callable[[int, int], bool],
               now: Callable[[], float] = time.time) -> CallState:
    if (call_dir / "exit.json").is_file():
        return "done"
    pids = read_pids(call_dir)
    if pids is not None:
        state: CallState = "running" if alive(pids["supervisorPid"], pids["supervisorCreated"]) else "lost"
    else:
        call = read_call(call_dir)
        young = call is not None and now() - datetime.fromisoformat(call["createdAt"]).timestamp() \
            < STARTUP_GRACE_SECONDS
        state = "running" if young else "lost"
    # the supervisor may have written exit.json and exited between the first check and the liveness probe
    if state == "lost" and (call_dir / "exit.json").is_file():
        return "done"
    return state


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, value: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
