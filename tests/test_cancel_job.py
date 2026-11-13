"""cancelJob (0.1.11): a running job's pwsh and every process it started are killed; the job ends done, cancelled."""

import re
import time
from pathlib import Path

import anyio

from mh_mcp_server.processes import process_alive
from support import call, connect, wait_one, write_home

SID = "20261026-210000-0000cc"
CHILD = "python -c \"import os, time; print('child=' + str(os.getpid()), flush=True); time.sleep(120)\"; 'never'"


def test_cancel_kills_a_running_job_and_the_process_it_started(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            header, _, is_error = await call(session, "startJob", sessionId=SID, command=CHILD, name="long")
            assert not is_error, header
            job_id = header["jobId"]
            header, text, _ = await wait_one(session, SID, job_id, maxSeconds=30, untilRegex=r"child=\d+")
            assert header.get("matched"), (header, text)
            child = int(re.search(r"child=(\d+)", text).group(1))
            assert process_alive(child, None), "the job's child runs"

            started = time.monotonic()
            header, _, is_error = await call(session, "cancelJob", sessionId=SID, jobId=job_id)
            assert not is_error, header
            assert (header["state"], header["cancelled"], header["killed"]) == ("done", True, True), header
            assert header["exitCode"] != 0, header
            assert time.monotonic() - started < 15
            deadline = time.monotonic() + 5
            while process_alive(child, None) and time.monotonic() < deadline:
                await anyio.sleep(0.2)
            assert not process_alive(child, None), "the job's child is killed with its shell"

            header, text, _ = await wait_one(session, SID, job_id, maxSeconds=5)
            assert header["state"] == "done" and header["cancelled"] is True, header
            assert "never" not in (text or "")

    anyio.run(main)


def test_cancel_reports_a_job_no_longer_running_and_kills_nothing(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            header, _, _ = await call(session, "startJob", sessionId=SID, command="'quick'")
            job_id = header["jobId"]
            header, _, _ = await wait_one(session, SID, job_id, maxSeconds=30)
            assert header["state"] == "done", header

            header, _, is_error = await call(session, "cancelJob", sessionId=SID, jobId=job_id)
            assert not is_error, header
            assert header == {"jobId": job_id, "state": "done", "cancelled": False}
            header, _, _ = await wait_one(session, SID, job_id, maxSeconds=1)
            assert "cancelled" not in header and header["exitCode"] == 0, header

    anyio.run(main)


def test_cancel_refuses_an_unknown_job_and_a_bad_session(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            header, _, is_error = await call(session, "cancelJob", sessionId=SID, jobId="9999")
            assert is_error and header["error"] == "JOB_NOT_FOUND", header
            header, _, is_error = await call(session, "cancelJob", sessionId="bad", jobId="0001")
            assert is_error and header["error"] == "BAD_SESSION_ID", header

    anyio.run(main)
