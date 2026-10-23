"""Helpers shared by the process-level tests: real pwsh, real detached supervisors, real call directories."""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Mapping

from mh_mcp_server.calls import allocate_call, read_exit, supervisor_argv, write_call
from mh_mcp_server.processes import spawn_detached
from mh_mcp_server.wrapper import render_wrapper

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
SID = "20261023-154207-0a1b2c"
PWSH = shutil.which("pwsh")
SHELL_ARGS = ("-NoLogo", "-NoProfile", "-NonInteractive", "-File")


def new_call_dir(home: Path) -> tuple[str, Path]:
    return allocate_call(home / "runs" / SID)


def start_ps1(home: Path, script: str, cwd: Path | None = None) -> Path:
    """A call directory whose command.ps1 is `script`, started under a detached supervisor."""
    _, call_dir = new_call_dir(home)
    ps1 = call_dir / "command.ps1"
    ps1.write_text(script, encoding="utf-8")
    start_argv(call_dir, [PWSH, *SHELL_ARGS, str(ps1)], cwd or home)
    return call_dir


def start_argv(call_dir: Path, argv: list[str], cwd: Path, env: Mapping[str, str] | None = None) -> None:
    write_call(call_dir, argv, cwd, datetime.now().astimezone())
    spawn_detached(supervisor_argv(call_dir), dict(os.environ) if env is None else env, cwd)


def wait_exit(call_dir: Path, seconds: float = 60.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        exit_record = read_exit(call_dir)
        if exit_record is not None:
            return exit_record
        time.sleep(0.05)
    raise TimeoutError(f"no exit.json in {call_dir} after {seconds} s")


def output_of(call_dir: Path) -> str:
    return (call_dir / "output.log").read_bytes().decode("utf-8", "replace")


def run_wrapped(home: Path, command: str, cwd: Path, env: Mapping[str, str] | None = None
                ) -> tuple[dict, str, dict | None, Path]:
    """`command` wrapped by render_wrapper and run to its end: (exit.json, output, state-out.json or None, callDir)."""
    _, call_dir = new_call_dir(home)
    state_out = call_dir / "state-out.json"
    ps1 = call_dir / "command.ps1"
    ps1.write_text(render_wrapper(command, state_out), encoding="utf-8")
    start_argv(call_dir, [PWSH, *SHELL_ARGS, str(ps1)], cwd, env)
    record = wait_exit(call_dir)
    state = json.loads(state_out.read_text(encoding="utf-8")) if state_out.exists() else None
    return record, output_of(call_dir), state, call_dir
