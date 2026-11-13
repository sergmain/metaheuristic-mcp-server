"""startJob and waitJob (plan 051, Phase 8).

A job never takes the session's foreground lock and never changes session state: it starts in the session's working
directory and environment, and its own cd and $env: changes are discarded (decision 10).

`waitJob` returns as soon as the job is done (or lost), a line after `sinceLine` matches `untilRegex`, or
`maxSeconds` have passed. `nextLine` is the number of lines read so far; passed back as `sinceLine`, two waits never
return the same line. The returned text is capped keeping the TAIL.

`cancelJob` (0.1.11, the user: "just add a new tool for canceling any jobs - the Processor already does that when wall
time is over"): kills a running call's pwsh and every process it started, after writing `cancel.json` beside it; the
supervisor outlives the kill and writes `exit.json` as for any end, so the call ends `done` with the killed shell's exit
code, and `waitJob` reports `cancelled`.

`waitJob` over several jobs (0.1.13, plan 056 Phase 2): a call names 1 - WAIT_JOBS_MAX jobs, each by its (sessionId,
jobId) pair with a `sinceLine` of its own, and waits on all of them in one server-side loop - one round trip instead of
one per job. mode "any" returns when one job is done or lost or matches `untilRegex`; mode "all" when every job is
done, lost or matched (a match settles a job without ending the wait). An unknown jobId is an entry's error, not a
refusal. The call has no sessionId of its own: its transcript block goes to every session it names.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import anyio
import anyio.to_thread

from mh_mcp_server.calls import _write_json_atomic, allocate_call, call_state, read_call, read_exit, read_pids, runs_dir
from mh_mcp_server.execution import Reply, Runtime, admit_call, refusal, spawn_call
from mh_mcp_server.output import Line, cap_tail, decode_line
from mh_mcp_server.processes import kill_tree
from mh_mcp_server.session_id import is_valid_session_id
from mh_mcp_server.session_state import load_state

_JOB_ID = re.compile(r"\d{4,}")
CANCEL_FILE = "cancel.json"
CANCEL_WAIT_SECONDS = 10
WAIT_JOBS_MAX = 20
WAIT_MODES = ("any", "all")


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


def cancel_job(rt: Runtime, session_id: str, job_id: str) -> Reply:
    """Kills running call `job_id` (a job, or an executeCommand promoted to one) with every process it started, and
    waits up to CANCEL_WAIT_SECONDS for its supervisor to record the end. A call no longer running is reported as it
    is, with nothing killed."""
    if not is_valid_session_id(session_id):
        return refusal("BAD_SESSION_ID")
    call_dir = runs_dir(rt.home, session_id) / job_id
    if _JOB_ID.fullmatch(job_id) is None or read_call(call_dir) is None:
        return refusal("JOB_NOT_FOUND", session_id, jobId=job_id)
    state = call_state(call_dir, rt.alive)
    if state != "running":
        return Reply({"jobId": job_id, "state": state, "cancelled": False}, None, False, None, session_id)
    pids = read_pids(call_dir)
    if pids is None:
        return refusal("BAD_ARGUMENTS", session_id, message="the job is still starting - retry in a moment")
    _write_json_atomic(call_dir / CANCEL_FILE, {"at": datetime.now().astimezone().isoformat(timespec="milliseconds")})
    killed = kill_tree(pids["shellPid"], pids.get("shellCreated"))
    deadline = time.monotonic() + CANCEL_WAIT_SECONDS
    while (state := call_state(call_dir, rt.alive)) == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
    header: dict[str, Any] = {"jobId": job_id, "state": state, "cancelled": True, "killed": killed}
    exit_record = read_exit(call_dir) if state == "done" else None
    if exit_record is not None:
        header["exitCode"] = exit_record.get("exit")
    return Reply(header, None, False, None, session_id)


async def wait_job(rt: Runtime, refs: Sequence[Mapping[str, Any]], mode: str = "any", max_seconds: int | None = None,
                   until_regex: str | None = None, max_lines: int | None = None) -> Reply:
    """Waits on every job of `refs` ({"sessionId", "jobId", "sinceLine"} each) in one loop, polling each job not yet
    settled every rt.poll_seconds, until `mode` is satisfied or `max_seconds` pass."""
    if not refs or len(refs) > WAIT_JOBS_MAX:
        return refusal("BAD_ARGUMENTS", message=f"jobs: 1 - {WAIT_JOBS_MAX}")
    bad = next((ref["sessionId"] for ref in refs if not is_valid_session_id(ref["sessionId"])), None)
    if bad is not None:
        return refusal("BAD_SESSION_ID", sessionId=bad)
    sessions = tuple(dict.fromkeys(ref["sessionId"] for ref in refs))
    wait = rt.config.default_foreground_seconds if max_seconds is None else max_seconds
    if wait > rt.config.max_foreground_seconds:
        return _refused(sessions, "TIMEOUT_TOO_LARGE", max=rt.config.max_foreground_seconds)
    if mode not in WAIT_MODES:
        return _refused(sessions, "BAD_ARGUMENTS", message='mode: "any" or "all"')
    if wait < 0 or any(ref["sinceLine"] < 0 for ref in refs) or (max_lines is not None and max_lines < 1):
        return _refused(sessions, "BAD_ARGUMENTS", message="maxSeconds and sinceLine must be >= 0, maxLines >= 1")
    try:
        pattern = re.compile(until_regex) if until_regex is not None else None
    except re.error as e:
        return _refused(sessions, "BAD_ARGUMENTS", message=f"untilRegex: {e}")

    waits = await anyio.to_thread.run_sync(_open_waits, rt, refs)
    started = time.monotonic()
    deadline = started + wait
    while True:
        await anyio.to_thread.run_sync(_poll, rt, waits, pattern)
        live = [w for w in waits if w.error is None]
        satisfied = any(w.settled for w in live) if mode == "any" else all(w.settled for w in live)
        if not live or satisfied:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await anyio.sleep(min(rt.poll_seconds, remaining))
    seconds = round(time.monotonic() - started, 2)
    reply = await anyio.to_thread.run_sync(_reply, rt, mode, seconds, waits,
                                           rt.config.max_lines if max_lines is None else max_lines)
    return replace(reply, session_id=sessions[0], session_ids=sessions[1:])


def _refused(sessions: tuple[str, ...], kind: str, **fields: Any) -> Reply:
    """A refusal whose transcript block goes to every session the call names."""
    return replace(refusal(kind, sessions[0], **fields), session_ids=sessions[1:])


class _Wait:
    """One job of a waitJob call: its call directory (None when there is no such job), its cursor, and where the wait
    left it."""

    def __init__(self, session_id: str, job_id: str, call_dir: Path | None, since_line: int) -> None:
        self.session_id = session_id
        self.job_id = job_id
        self.call_dir = call_dir
        self.cursor = _Cursor(since_line)
        self.state = "running"
        self.matched: str | None = None

    @property
    def error(self) -> str | None:
        return "JOB_NOT_FOUND" if self.call_dir is None else None

    @property
    def settled(self) -> bool:
        """Done, lost, or stopped at a matching line - nothing more is read for it in this call."""
        return self.matched is not None or self.state != "running"


def _open_waits(rt: Runtime, refs: Sequence[Mapping[str, Any]]) -> list[_Wait]:
    waits: list[_Wait] = []
    for ref in refs:
        session_id, job_id = ref["sessionId"], ref["jobId"]
        call_dir = runs_dir(rt.home, session_id) / job_id
        known = _JOB_ID.fullmatch(job_id) is not None and read_call(call_dir) is not None
        waits.append(_Wait(session_id, job_id, call_dir if known else None, ref["sinceLine"]))
    return waits


def _poll(rt: Runtime, waits: list[_Wait], pattern: re.Pattern | None) -> None:
    """One round over the jobs not yet settled: the state first, then the lines added since the last round."""
    for w in waits:
        if w.error is not None or w.settled:
            continue
        w.state = call_state(w.call_dir, rt.alive)
        # the last line counts without its line ending only once nothing can append to it
        w.matched = w.cursor.advance(w.call_dir / "output.log", w.state != "running", pattern)


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


def _reply(rt: Runtime, mode: str, seconds: float, waits: list[_Wait], max_lines: int) -> Reply:
    """Header {"mode", "seconds", "jobs": [...]}; content 1 one section per job with new lines - `== <sessionId>/<jobId>
    <state>` and then its tail, `max_lines` per job and the reply's byte budget split evenly across those jobs."""
    budget = rt.config.max_bytes // max(1, sum(1 for w in waits if w.cursor.collected))
    entries: list[dict[str, Any]] = []
    text: list[str] = []
    for w in waits:
        if w.error is not None:
            entries.append({"sessionId": w.session_id, "jobId": w.job_id, "error": w.error})
            continue
        kept, omitted = cap_tail(w.cursor.collected, max_lines, budget)
        next_line = max(w.cursor.count, w.cursor.since_line)
        entry: dict[str, Any] = {"sessionId": w.session_id, "jobId": w.job_id, "state": w.state}
        exit_record = read_exit(w.call_dir) if w.state == "done" else None
        if exit_record is not None:
            entry["exitCode"] = exit_record.get("exit")
        if (w.call_dir / CANCEL_FILE).is_file():
            entry["cancelled"] = True
        entry["fromLine"] = kept[0].index + 1 if kept else next_line + 1
        entry["nextLine"] = next_line
        if w.matched is not None:
            entry["matched"] = w.matched
        if omitted:
            entry["omitted"] = omitted
        bad = sum(line.bad for line in kept)
        if bad:
            entry["encodingErrors"] = bad
        entries.append(entry)
        if kept:
            text.append(f"== {w.session_id}/{w.job_id} {w.state}")
            text.extend(line.text for line in kept)
    return Reply({"mode": mode, "seconds": seconds, "jobs": entries}, "\n".join(text) or None)
