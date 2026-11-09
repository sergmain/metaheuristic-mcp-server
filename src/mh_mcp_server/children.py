"""A child process that runs inside one tool call (0.1.12: sqlQuery, gitCommit) - unlike a command, which runs
under a detached supervisor and outlives its call as a job.

Such a child is bounded by the call: stdin is written and closed at once (nothing ever waits on it), and a child still
running at the deadline is killed and reported `timed_out` - a call returns within its 58 s whatever the child does.
It never leaves the server's job object, so it also ends with the server. On Windows it gets no console window
(the server, started by Desktop, has none to share).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class Finished:
    exit_code: int | None      # None when the child was killed at the deadline
    stdout: bytes              # empty when stdout went to a file
    stderr: bytes
    timed_out: bool


def env_value(env: Mapping[str, str], name: str) -> str | None:
    """`name`'s value in `env`, the name compared case-insensitively as Windows does; None when absent."""
    wanted = name.casefold()
    return next((value for key, value in env.items() if key.casefold() == wanted), None)


def without(env: Mapping[str, str], drop) -> dict[str, str]:
    """`env` without every name for which `drop(name)` is true."""
    return {key: value for key, value in env.items() if not drop(key)}


def run_child(argv: Sequence[str], env: Mapping[str, str], cwd: Path, stdin: bytes, timeout: float,
              stdout_path: Path | None = None) -> Finished:
    """Runs `argv` to its end or to `timeout` seconds; stdout into `stdout_path` when given (the full output stays on
    disk, not in the server's memory), else captured."""
    if timeout <= 0:
        return Finished(None, b"", b"", True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    out = open(stdout_path, "wb") if stdout_path is not None else subprocess.PIPE
    try:
        proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=out, stderr=subprocess.PIPE, env=dict(env),
                                cwd=str(cwd), creationflags=flags)
        try:
            stdout, stderr = proc.communicate(stdin, timeout=timeout)
            return Finished(proc.returncode, stdout or b"", stderr or b"", False)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return Finished(None, stdout or b"", stderr or b"", True)
    finally:
        if stdout_path is not None:
            out.close()
