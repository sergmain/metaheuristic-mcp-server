"""The supervisor of one call: `python supervisor.py <callDir>` (plan 051, decision 7, Phase 5).

Standard library only, started by file path. Reads `call.json` ({"argv", "cwd", "createdAt"}), starts argv with
stdin = NUL and one file handle, `output.log`, for both stdout and stderr, records `pid.json`, waits for the process
to exit and writes `exit.json` ({"exit", "endedAt", "seconds"}) as `exit.json.tmp` + `os.replace`.

The command's environment is this process's environment: the server spawns the supervisor with it, so it never lands
in a file. The supervisor waits for the process, not for the end of its output, so a detached grandchild that holds
`output.log` open does not delay `exit.json`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from processes import process_created  # noqa: E402  (stdlib-only sibling, imported from this directory)

CANNOT_START_EXIT = 127


def write_json_atomic(path: Path, value: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def supervise(call_dir: Path) -> int:
    call = json.loads((call_dir / "call.json").read_text(encoding="utf-8"))
    started = time.monotonic()
    # the supervisor has no console, so a console program it starts would open a window of its own
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with open(call_dir / "output.log", "wb") as log:
        try:
            proc = subprocess.Popen(call["argv"], cwd=call["cwd"], stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, creationflags=flags)
        except OSError as e:
            log.write(f"[mh-mcp] cannot start {call['argv'][0]}: {e}\n".encode("utf-8"))
            log.flush()
            code = CANNOT_START_EXIT
        else:
            write_json_atomic(call_dir / "pid.json", {
                "supervisorPid": os.getpid(),
                "supervisorCreated": process_created(os.getpid()),
                "shellPid": proc.pid,
                "shellCreated": process_created(proc.pid),
            })
            code = proc.wait()
    write_json_atomic(call_dir / "exit.json", {
        "exit": code,
        "endedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
        "seconds": round(time.monotonic() - started, 3),
    })
    return code


if __name__ == "__main__":
    supervise(Path(sys.argv[1]))
