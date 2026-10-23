"""startJob and waitJob (plan 051, Phase 8).

A job never takes the session's foreground lock and never changes session state: it starts in the session's working
directory and environment, and its own cd and $env: changes are discarded (decision 10).

`waitJob` returns as soon as the job is done (or lost), a line after `sinceLine` matches `untilRegex`, or
`maxSeconds` have passed. `nextLine` is the number of lines read so far; passed back as `sinceLine`, two waits never
return the same line. The returned text is capped keeping the TAIL.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread

from mh_mcp_server.calls import allocate_call, call_state, read_call, read_exit, runs_dir
from mh_mcp_server.execution import Reply, Runtime, admit_call, refusal, spawn_call
from mh_mcp_server.output import Line, cap_tail, decode_line
from mh_mcp_server.session_id import is_valid_session_id
from mh_mcp_server.session_state import load_state

_JOB_ID = re.compile(r"\d{4,}")


def start_job(rt: Runtime, session_id: str, command: str, cwd: str | None = None, name: str | None = None) -> Reply:
    if not is_valid_session_id(session_id):
        return refusal("BAD_SESSION_ID")
    state = load_state(rt.home, session_id, rt.config.default_cwd)
    resolved = admit_call(rt, state, command, cwd)
    if isinstance(resolved, Reply):
        return Reply(resolved.header, None, True, None, session_id)
    job_cwd, notes = resolved
    call_id, call_dir = allocate_call(runs_dir(rt.home, session_id))
    spawn_call(rt, session_id, call_id, call_dir, command, job_cwd, state, "job", name)
    header: dict[str, Any] = {"jobId": call_id, "logPath": str(call_dir / "output.log")}
    if notes:
        header["notes"] = notes
    return Reply(header, None, False, call_id, session_id)


async def wait_job(rt: Runtime, session_id: str, job_id: str, max_seconds: int | None = None, since_line: int = 0,
                   until_regex: str | None = None, max_lines: int | None = None) -> Reply:
    if not is_valid_session_id(session_id):
        return refusal("BAD_SESSION_ID")
    wait = rt.config.default_foreground_seconds if max_seconds is None else max_seconds
    if wait > rt.config.max_foreground_seconds:
        return refusal("TIMEOUT_TOO_LARGE", session_id, max=rt.config.max_foreground_seconds)
    if wait < 0 or since_line < 0 or (max_lines is not None and max_lines < 1):
        return refusal("BAD_ARGUMENTS", session_id, message="maxSeconds and sinceLine must be >= 0, maxLines >= 1")
    try:
        pattern = re.compile(until_regex) if until_regex is not None else None
    except re.error as e:
        return refusal("BAD_ARGUMENTS", session_id, message=f"untilRegex: {e}")
    call_dir = runs_dir(rt.home, session_id) / job_id
    if _JOB_ID.fullmatch(job_id) is None or await anyio.to_thread.run_sync(read_call, call_dir) is None:
        return refusal("JOB_NOT_FOUND", session_id, jobId=job_id)

    log = call_dir / "output.log"
    cursor = _Cursor(since_line)
    deadline = time.monotonic() + wait
    matched: str | None = None
    while True:
        state = await anyio.to_thread.run_sync(call_state, call_dir, rt.alive)
        # the last line counts without its line ending only once nothing can append to it
        matched = await anyio.to_thread.run_sync(cursor.advance, log, state != "running", pattern)
        if matched is not None or state != "running":
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await anyio.sleep(min(rt.poll_seconds, remaining))
    return await anyio.to_thread.run_sync(_reply, rt, session_id, job_id, call_dir, state, cursor, matched,
                                          rt.config.max_lines if max_lines is None else max_lines)


class _Cursor:
    """Reads `output.log` incrementally within one wait: lines are counted from the start once, then only new bytes
    are read. Lines before `since_line` are skipped, not kept."""

    def __init__(self, since_line: int) -> None:
        self.since_line = since_line
        self.offset = 0
        self.count = 0
        self.collected: list[Line] = []

    def advance(self, path: Path, include_partial: bool, pattern: re.Pattern | None) -> str | None:
        """Reads the complete lines added since the last call; the first one (after since_line) that matches
        `pattern`, at which the cursor stops."""
        try:
            f = open(path, "rb")
        except FileNotFoundError:
            return None
        with f:
            f.seek(self.offset)
            for raw in f:
                if not raw.endswith(b"\n") and not include_partial:
                    return None
                self.offset += len(raw)
                index = self.count
                self.count += 1
                if index < self.since_line:
                    continue
                text, bad = decode_line(raw)
                self.collected.append(Line(index, text, bad))
                if pattern is not None and pattern.search(text):
                    return text
        return None


def _reply(rt: Runtime, session_id: str, job_id: str, call_dir: Path, state: str, cursor: _Cursor,
           matched: str | None, max_lines: int) -> Reply:
    kept, omitted = cap_tail(cursor.collected, max_lines, rt.config.max_bytes)
    next_line = max(cursor.count, cursor.since_line)
    header: dict[str, Any] = {"jobId": job_id, "state": state}
    exit_record = read_exit(call_dir) if state == "done" else None
    if exit_record is not None:
        header["exitCode"] = exit_record.get("exit")
        header["seconds"] = exit_record.get("seconds")
    else:
        call = read_call(call_dir) or {}
        created = call.get("createdAt")
        header["seconds"] = round(time.time() - datetime.fromisoformat(created).timestamp(), 2) if created else None
    header["fromLine"] = kept[0].index + 1 if kept else next_line + 1
    header["nextLine"] = next_line
    if matched is not None:
        header["matched"] = matched
    if omitted:
        header["omitted"] = omitted
    bad = sum(line.bad for line in kept)
    if bad:
        header["encodingErrors"] = bad
    return Reply(header, "\n".join(line.text for line in kept) or None, False, None, session_id)
