import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import anyio

from mh_mcp_server.calls import write_call
from mh_mcp_server.jobs import WAIT_JOBS_MAX
from mh_mcp_server.processes import process_created
from support import call, connect, connect_plain, job_sections, wait_one, write_home

SID = "20261023-170000-0000aa"
SID_B = "20261112-170000-0000bb"


def test_tools_list_has_the_job_tools(tmp_path: Path):
    # Phase 8 had five tools; the exact list is pinned by test_tools_list_is_exactly_the_tools_of_this_version
    async def main():
        async with connect(write_home(tmp_path)) as session:
            names = [t.name for t in (await session.list_tools()).tools]
            assert {"startJob", "waitJob"} <= set(names)

    anyio.run(main)


def test_wait_job_description_names_the_job_limit_and_has_no_session_id_of_its_own(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            tool = next(t for t in (await session.list_tools()).tools if t.name == "waitJob")
            assert f"1-{WAIT_JOBS_MAX} jobs" in tool.description
            assert "jobs" in tool.inputSchema["properties"] and "sessionId" not in tool.inputSchema["properties"]

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
            header, text, is_error = await wait_one(session, SID, job_id, maxSeconds=30)
            assert not is_error, header
            assert header["state"] == "done" and header["exitCode"] == 0
            assert text == "done"
            assert header["nextLine"] == 1 and header["fromLine"] == 1

    anyio.run(main)


def test_until_regex_returns_early_on_a_match_while_the_job_still_runs(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            header, _, _ = await call(session, "startJob", sessionId=SID, command="'starting'; 'ready'; Start-Sleep 6; 'end'")
            job_id = header["jobId"]
            started = time.monotonic()
            header, text, _ = await wait_one(session, SID, job_id, maxSeconds=30, untilRegex="^read")
            assert time.monotonic() - started < 5
            assert header["state"] == "running" and header["matched"] == "ready"
            assert text == "starting\nready" and header["nextLine"] == 2
            header, text, _ = await wait_one(session, SID, job_id, since_line=header["nextLine"], maxSeconds=30)
            assert header["state"] == "done" and text == "end" and header["fromLine"] == 3

    anyio.run(main)


def test_two_waits_with_the_next_line_cursor_return_disjoint_lines(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            header, _, _ = await call(session, "startJob", sessionId=SID,
                                      command="1..8 | ForEach-Object { \"n$_\"; Start-Sleep -Milliseconds 400 }")
            job_id = header["jobId"]
            first, text1, _ = await wait_one(session, SID, job_id, maxSeconds=1)
            assert first["state"] == "running"
            second, text2, _ = await wait_one(session, SID, job_id, since_line=first["nextLine"], maxSeconds=30)
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
            header, text, is_error = await wait_one(session, SID, "0001", maxSeconds=30)
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
            await wait_one(session, SID, quick["jobId"], maxSeconds=30)
            header, _, _ = await call(session, "getSession", sessionId=SID)
            jobs = {j["jobId"]: j for j in header["jobs"]}
            assert jobs[quick["jobId"]]["state"] == "done" and jobs[quick["jobId"]]["exitCode"] == 0
            assert jobs[quick["jobId"]]["name"] == "quick"
            assert jobs[slow["jobId"]]["state"] == "running" and jobs[slow["jobId"]]["exitCode"] is None
            assert header["foreground"] is None
            await wait_one(session, SID, slow["jobId"], maxSeconds=30)

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
            await wait_one(session, SID, job["jobId"], maxSeconds=30)
            header, _, _ = await call(session, "getSession", sessionId=SID)
            assert header["envSet"] == [] and header["cwd"] == str(tmp_path)

    anyio.run(main)


def test_wait_job_refusals(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            # an unknown job is its entry's error, not a refusal - and with nothing to wait on the call returns at once
            started = time.monotonic()
            header, text, is_error = await call(session, "waitJob", maxSeconds=30,
                                                jobs=[{"sessionId": SID, "jobId": "0099"},
                                                      {"sessionId": SID, "jobId": "../x"}])
            assert time.monotonic() - started < 3
            assert not is_error and text is None
            assert header["jobs"] == [{"sessionId": SID, "jobId": "0099", "error": "JOB_NOT_FOUND"},
                                      {"sessionId": SID, "jobId": "../x", "error": "JOB_NOT_FOUND"}]
            assert (await wait_one(session, SID, "0001", maxSeconds=100))[0] == {"error": "TIMEOUT_TOO_LARGE", "max": 58}
            assert (await wait_one(session, "bad", "0001"))[0] == {"error": "BAD_SESSION_ID", "sessionId": "bad"}
            assert (await wait_one(session, SID, "0001", mode="first"))[0]["error"] == "BAD_ARGUMENTS"
            assert (await wait_one(session, SID, "0001", since_line=-1))[0]["error"] == "BAD_ARGUMENTS"
            header, _, is_error = await call(session, "waitJob", jobs=[])
            assert is_error and header["error"] == "BAD_ARGUMENTS"
            header, _, is_error = await call(session, "waitJob",
                                             jobs=[{"sessionId": SID, "jobId": f"{i:04d}"} for i in range(1, 22)])
            assert is_error and header["error"] == "BAD_ARGUMENTS"
            job, _, _ = await call(session, "startJob", sessionId=SID, command="'x'")
            header, _, is_error = await wait_one(session, SID, job["jobId"], untilRegex="(")
            assert is_error and header["error"] == "BAD_ARGUMENTS"

    anyio.run(main)


# ==================== several jobs in one call (0.1.13, plan 056 Phase 2) ====================

def test_mode_any_returns_with_the_first_job_done_and_mode_all_with_both(tmp_path: Path):
    home = write_home(tmp_path)

    async def main():
        async with connect(home) as session:
            a, _, _ = await call(session, "startJob", sessionId=SID, command="Start-Sleep 1; 'a'")
            b, _, _ = await call(session, "startJob", sessionId=SID_B, command="Start-Sleep 20; 'b'; exit 3")
            assert a["jobId"] == b["jobId"] == "0001"   # a jobId is unique only within its session
            started = time.monotonic()
            header, text, is_error = await call(session, "waitJob", mode="any", maxSeconds=30,
                                                jobs=[{"sessionId": SID, "jobId": a["jobId"]},
                                                      {"sessionId": SID_B, "jobId": b["jobId"]}])
            assert time.monotonic() - started < 10
            assert not is_error and header["mode"] == "any", header
            first, second = header["jobs"]
            assert (first["sessionId"], first["jobId"], first["state"], first["exitCode"]) == (SID, "0001", "done", 0)
            assert (second["sessionId"], second["state"]) == (SID_B, "running") and "exitCode" not in second
            assert text == f"== {SID}/0001 done\na"

            header, text, is_error = await call(session, "waitJob", mode="all", maxSeconds=40,
                                                jobs=[{"sessionId": SID, "jobId": "0001", "sinceLine": first["nextLine"]},
                                                      {"sessionId": SID_B, "jobId": "0001",
                                                       "sinceLine": second["nextLine"]}])
            assert not is_error and header["mode"] == "all", header
            assert [(j["sessionId"], j["state"], j["exitCode"]) for j in header["jobs"]] == \
                [(SID, "done", 0), (SID_B, "done", 3)]
            assert text == f"== {SID_B}/0001 done\nb"

        # each session's transcript holds both waitJob blocks: one task's complete chain of calls per session
        for session_id in (SID, SID_B):
            transcript = (home / "sessions" / f"{session_id}.txt").read_text(encoding="utf-8")
            assert transcript.count("| waitJob |") == 2, session_id

    anyio.run(main)


def test_two_waits_over_two_jobs_return_disjoint_lines_per_job(tmp_path: Path):
    def counting(prefix: str) -> str:
        return f"1..6 | ForEach-Object {{ \"{prefix}$_\"; Start-Sleep -Milliseconds 400 }}"

    async def main():
        async with connect(write_home(tmp_path)) as session:
            a, _, _ = await call(session, "startJob", sessionId=SID, command=counting("a"))
            b, _, _ = await call(session, "startJob", sessionId=SID_B, command=counting("b"))
            refs = [{"sessionId": SID, "jobId": a["jobId"]}, {"sessionId": SID_B, "jobId": b["jobId"]}]
            first, text1, _ = await call(session, "waitJob", jobs=refs, mode="all", maxSeconds=1)
            assert [j["state"] for j in first["jobs"]] == ["running", "running"], first
            refs = [{**ref, "sinceLine": entry["nextLine"]} for ref, entry in zip(refs, first["jobs"])]
            second, text2, _ = await call(session, "waitJob", jobs=refs, mode="all", maxSeconds=30)
            assert [(j["state"], j["exitCode"]) for j in second["jobs"]] == [("done", 0), ("done", 0)], second
            for ref, prefix, entry1, entry2 in zip(refs, "ab", first["jobs"], second["jobs"]):
                key = f"{ref['sessionId']}/{ref['jobId']}"
                one = job_sections(text1).get(key, "").splitlines()
                two = job_sections(text2).get(key, "").splitlines()
                assert not set(one) & set(two)
                assert one + two == [f"{prefix}{i}" for i in range(1, 7)]
                assert entry2["nextLine"] == 6 and entry2["fromLine"] == entry1["nextLine"] + 1

    anyio.run(main)


def test_an_unknown_job_is_its_entrys_error_while_the_other_job_is_answered(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            job, _, _ = await call(session, "startJob", sessionId=SID, command="'here'")
            header, text, is_error = await call(session, "waitJob", mode="all", maxSeconds=30,
                                                jobs=[{"sessionId": SID, "jobId": "0099"},
                                                      {"sessionId": SID, "jobId": job["jobId"]}])
            assert not is_error, header
            missing, found = header["jobs"]
            assert missing == {"sessionId": SID, "jobId": "0099", "error": "JOB_NOT_FOUND"}
            assert (found["state"], found["exitCode"]) == ("done", 0)
            assert text == f"== {SID}/{job['jobId']} done\nhere"

    anyio.run(main)


def test_mode_all_a_match_settles_its_job_without_ending_the_wait(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            a, _, _ = await call(session, "startJob", sessionId=SID, command="'ready'; Start-Sleep 15; 'late'")
            b, _, _ = await call(session, "startJob", sessionId=SID_B, command="Start-Sleep 3; 'b'")
            started = time.monotonic()
            header, text, _ = await call(session, "waitJob", mode="all", maxSeconds=30, untilRegex="^ready",
                                         jobs=[{"sessionId": SID, "jobId": a["jobId"]},
                                               {"sessionId": SID_B, "jobId": b["jobId"]}])
            assert time.monotonic() - started < 12
            first, second = header["jobs"]
            assert (first["state"], first["matched"]) == ("running", "ready"), first
            assert (second["state"], second["exitCode"]) == ("done", 0) and "matched" not in second, second
            assert job_sections(text) == {f"{SID}/{a['jobId']}": "ready", f"{SID_B}/{b['jobId']}": "b"}
            await wait_one(session, SID, a["jobId"], since_line=first["nextLine"], maxSeconds=30)

    anyio.run(main)
