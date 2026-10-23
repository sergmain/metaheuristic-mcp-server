import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import anyio

from mh_mcp_server.calls import write_call
from mh_mcp_server.processes import process_created
from support import call, connect, connect_plain, write_home

SID = "20261023-170000-0000aa"


def test_tools_list_has_the_job_tools(tmp_path: Path):
    # Phase 8 had five tools; the exact list is pinned by test_tools_list_is_exactly_the_tools_of_this_version
    async def main():
        async with connect(write_home(tmp_path)) as session:
            names = [t.name for t in (await session.list_tools()).tools]
            assert {"startJob", "waitJob"} <= set(names)

    anyio.run(main)


def test_a_job_started_through_server_1_is_waited_on_through_server_2_after_server_1_exited(tmp_path: Path):
    # ⚠️ server 1 runs as a plain child process: the SDK's Windows stdio_client holds its server in a
    # KILL_ON_JOB_CLOSE job object without BREAKAWAY_OK, which would kill the job with server 1 (plan 051 Phase 7
    # finding). What this test proves is the server's own property: nothing of a job depends on the server.
    home = write_home(tmp_path)

    async def main():
        async with connect_plain(home, tmp_path / "server1.err") as (server1, session):
            header, _, is_error = await call(session, "startJob", sessionId=SID, command="Start-Sleep 4; 'done'",
                                             name="slow")
            assert not is_error, header
            job_id = header["jobId"]
        assert server1.returncode is not None   # server 1 exited after its stdin was closed
        async with connect(home) as session:
            header, text, is_error = await call(session, "waitJob", sessionId=SID, jobId=job_id, maxSeconds=30)
            assert not is_error, header
            assert header["state"] == "done" and header["exitCode"] == 0
            assert text == "done"
            assert header["nextLine"] == 1 and header["fromLine"] == 1

    anyio.run(main)


def test_until_regex_returns_early_on_a_match_while_the_job_still_runs(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            header, _, _ = await call(session, "startJob", sessionId=SID, command="'starting'; 'ready'; Start-Sleep 6; 'end'")
            started = time.monotonic()
            header, text, _ = await call(session, "waitJob", sessionId=SID, jobId=header["jobId"], maxSeconds=30,
                                         untilRegex="^read")
            assert time.monotonic() - started < 5
            assert header["state"] == "running" and header["matched"] == "ready"
            assert text == "starting\nready" and header["nextLine"] == 2
            header, text, _ = await call(session, "waitJob", sessionId=SID, jobId=header["jobId"], maxSeconds=30,
                                         sinceLine=header["nextLine"])
            assert header["state"] == "done" and text == "end" and header["fromLine"] == 3

    anyio.run(main)


def test_two_waits_with_the_next_line_cursor_return_disjoint_lines(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            header, _, _ = await call(session, "startJob", sessionId=SID,
                                      command="1..8 | ForEach-Object { \"n$_\"; Start-Sleep -Milliseconds 400 }")
            job_id = header["jobId"]
            first, text1, _ = await call(session, "waitJob", sessionId=SID, jobId=job_id, maxSeconds=1)
            assert first["state"] == "running"
            second, text2, _ = await call(session, "waitJob", sessionId=SID, jobId=job_id, maxSeconds=30,
                                          sinceLine=first["nextLine"])
            assert second["state"] == "done" and second["exitCode"] == 0
            one = text1.splitlines() if text1 else []
            two = text2.splitlines() if text2 else []
            assert not set(one) & set(two)
            assert one + two == [f"n{i}" for i in range(1, 9)]
            assert second["nextLine"] == 8 and second["fromLine"] == first["nextLine"] + 1

    anyio.run(main)


def dead_process() -> tuple[int, int]:
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    created = process_created(proc.pid)
    proc.stdin.close()
    proc.wait(timeout=30)
    return proc.pid, created


def test_a_call_directory_with_a_dead_supervisor_and_no_exit_json_waits_as_lost_at_once(tmp_path: Path):
    home = write_home(tmp_path)
    call_dir = home / "runs" / SID / "0001"
    call_dir.mkdir(parents=True)
    write_call(call_dir, ["pwsh"], tmp_path, datetime.now().astimezone() - timedelta(minutes=5),
               {"kind": "job", "name": "ghost"})
    pid, created = dead_process()
    (call_dir / "pid.json").write_text(json.dumps({"supervisorPid": pid, "supervisorCreated": created,
                                                    "shellPid": pid, "shellCreated": created}), encoding="utf-8")
    (call_dir / "output.log").write_bytes(b"half\r\n")

    async def main():
        async with connect(home) as session:
            started = time.monotonic()
            header, text, is_error = await call(session, "waitJob", sessionId=SID, jobId="0001", maxSeconds=30)
            assert time.monotonic() - started < 3
            assert not is_error and header["state"] == "lost" and "exitCode" not in header
            assert text == "half"
            header, _, _ = await call(session, "getSession", sessionId=SID)
            assert header["jobs"][0]["state"] == "lost" and header["jobs"][0]["name"] == "ghost"

    anyio.run(main)


def test_get_session_lists_the_jobs_with_their_states(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            quick, _, _ = await call(session, "startJob", sessionId=SID, command="'x'", name="quick")
            slow, _, _ = await call(session, "startJob", sessionId=SID, command="Start-Sleep 5", name="slow")
            await call(session, "waitJob", sessionId=SID, jobId=quick["jobId"], maxSeconds=30)
            header, _, _ = await call(session, "getSession", sessionId=SID)
            jobs = {j["jobId"]: j for j in header["jobs"]}
            assert jobs[quick["jobId"]]["state"] == "done" and jobs[quick["jobId"]]["exitCode"] == 0
            assert jobs[quick["jobId"]]["name"] == "quick"
            assert jobs[slow["jobId"]]["state"] == "running" and jobs[slow["jobId"]]["exitCode"] is None
            assert header["foreground"] is None
            await call(session, "waitJob", sessionId=SID, jobId=slow["jobId"], maxSeconds=30)

    anyio.run(main)


def test_a_job_never_changes_the_session_and_never_takes_the_foreground(tmp_path: Path):
    (tmp_path / "sub").mkdir()

    async def main():
        async with connect(write_home(tmp_path)) as session:
            job, _, _ = await call(session, "startJob", sessionId=SID,
                                   command="$env:MCP_J = '1'; Set-Location sub; Start-Sleep 2")
            # the foreground is free while the job runs
            header, text, is_error = await call(session, "executeCommand", sessionId=SID, command="'fg'")
            assert not is_error and text == "fg"
            await call(session, "waitJob", sessionId=SID, jobId=job["jobId"], maxSeconds=30)
            header, _, _ = await call(session, "getSession", sessionId=SID)
            assert header["envSet"] == [] and header["cwd"] == str(tmp_path)

    anyio.run(main)


def test_wait_job_refusals(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            assert (await call(session, "waitJob", sessionId=SID, jobId="0099"))[0] == \
                {"error": "JOB_NOT_FOUND", "jobId": "0099"}
            assert (await call(session, "waitJob", sessionId=SID, jobId="../x"))[0]["error"] == "JOB_NOT_FOUND"
            assert (await call(session, "waitJob", sessionId=SID, jobId="0001", maxSeconds=100))[0] == \
                {"error": "TIMEOUT_TOO_LARGE", "max": 58}
            job, _, _ = await call(session, "startJob", sessionId=SID, command="'x'")
            header, _, is_error = await call(session, "waitJob", sessionId=SID, jobId=job["jobId"], untilRegex="(")
            assert is_error and header["error"] == "BAD_ARGUMENTS"

    anyio.run(main)
