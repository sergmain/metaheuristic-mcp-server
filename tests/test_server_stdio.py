import json
import re
import time
from pathlib import Path

import anyio
import anyio.to_thread

from mh_mcp_server import __version__
from mh_mcp_server.session_id import is_valid_session_id
from support import REPO, SECRET_VALUE, SRC, call, connect, output_of, wait_exit, write_home

SID_A = "20261023-160000-00000a"
SID_B = "20261023-160000-00000b"


def test_two_sessions_run_concurrently_on_one_connection(tmp_path: Path):
    """The phase's gate (plan 051 Phase 7 ❓): B returns while A is still running."""
    home = write_home(tmp_path)
    done: dict[str, float] = {}

    async def main():
        async with connect(home) as session:
            start = time.monotonic()

            async def run(sid: str, command: str, key: str):
                header, text, is_error = await call(session, "executeCommand", sessionId=sid, command=command)
                assert not is_error and header["exitCode"] == 0, header
                assert text == key
                done[key] = time.monotonic()

            async with anyio.create_task_group() as tg:
                tg.start_soon(run, SID_A, "Start-Sleep 6; 'A'", "A")
                await anyio.sleep(0.3)
                b_started = time.monotonic()
                tg.start_soon(run, SID_B, "'B'", "B")
            assert done["B"] - b_started < 3
            assert done["B"] < done["A"]
            assert done["A"] - start >= 6

    anyio.run(main)


def test_tools_list_is_exactly_the_three_tools_of_phase_7(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            tools = await session.list_tools()
            assert sorted(t.name for t in tools.tools) == ["executeCommand", "getSession", "getSessionId"]

    anyio.run(main)


def test_the_same_session_issuing_two_concurrent_commands_gets_one_session_busy(tmp_path: Path):
    results: list[tuple[dict, str | None, bool]] = []

    async def main():
        async with connect(write_home(tmp_path)) as session:
            async def run(command: str):
                results.append(await call(session, "executeCommand", sessionId=SID_A, command=command))

            async with anyio.create_task_group() as tg:
                tg.start_soon(run, "Start-Sleep 4; 'first'")
                await anyio.sleep(1.0)
                tg.start_soon(run, "'second'")

    anyio.run(main)
    refused = [r for r in results if r[2]]
    finished = [r for r in results if not r[2]]
    assert len(refused) == 1 and len(finished) == 1
    assert refused[0][0] == {"error": "SESSION_BUSY", "runningCallId": finished[0][0]["callId"]}
    assert finished[0][1] == "first"


def test_a_command_outliving_its_timeout_is_promoted_to_a_job_and_finishes(tmp_path: Path):
    home = write_home(tmp_path)

    async def main():
        async with connect(home) as session:
            header, _, is_error = await call(session, "executeCommand", sessionId=SID_A,
                                             command="Start-Sleep 5; 'late'", timeoutSeconds=2)
            assert not is_error
            assert header["state"] == "running" and header["jobId"] == header["callId"]
            assert 2 <= header["seconds"] < 4
            # Waited for while still connected: the SDK's Windows stdio_client holds the server in a
            # KILL_ON_JOB_CLOSE job object without BREAKAWAY_OK, so a disconnect would kill the job's supervisor.
            call_dir = home / "runs" / SID_A / header["jobId"]
            await anyio.to_thread.run_sync(wait_exit, call_dir, 20.0)
            return header

    header = anyio.run(main)
    call_dir = home / "runs" / SID_A / header["jobId"]
    record = wait_exit(call_dir, seconds=1)
    assert record["exit"] == 0
    assert output_of(call_dir).strip() == "late"
    assert Path(header["logPath"]) == call_dir / "output.log"


def test_the_two_command_shapes_of_the_request(tmp_path: Path):
    script = tmp_path / "one_line.py"
    script.write_text("import json, sys\nprint(json.dumps({'args': sys.argv[1:], 'text': 'Привет'}, "
                      "ensure_ascii=False))\n", encoding="utf-8")

    async def main():
        async with connect(write_home(tmp_path)) as session:
            header, text, _ = await call(
                session, "executeCommand", sessionId=SID_A, cwd=str(REPO),
                command="$p = Get-Content tests/fixtures/progress.json -Raw | ConvertFrom-Json; "
                        "\"done=$($p.done) failed=$($p.failed) current=$($p.current)\"")
            assert header["exitCode"] == 0, text
            assert text == "done=12 failed=1 current=RgSnapshotTest"
            header, text, _ = await call(session, "executeCommand", sessionId=SID_A,
                                         command=f"python {script} a b")
            assert header["exitCode"] == 0, text
            assert json.loads(text) == {"args": ["a", "b"], "text": "Привет"}

    anyio.run(main)


def test_the_transcript_holds_one_block_per_call_with_secrets_masked(tmp_path: Path):
    home = write_home(tmp_path)

    async def main():
        async with connect(home) as session:
            await call(session, "getSession", sessionId=SID_A)
            header, text, _ = await call(session, "executeCommand", sessionId=SID_A,
                                         command="\"secret=$env:MCP_TEST_SECRET\"")
            assert text == "secret=***"
            await call(session, "executeCommand", sessionId=SID_A, command=f"'{SECRET_VALUE}'")
            await call(session, "executeCommand", sessionId="not-a-session", command="'x'")
            return header

    header = anyio.run(main)
    text = (home / "sessions" / f"{SID_A}.txt").read_bytes().decode("utf-8")
    heads = re.findall(r"(?m)^=== .*$", text)
    assert len(heads) == 3
    assert " | getSession | " in heads[0]
    assert f"| call {header['callId']} | executeCommand |" in heads[1]
    assert SECRET_VALUE not in text
    assert "secret=***" in text and "'***'" in text
    assert "\r" not in text and not text.startswith("\ufeff")


def test_two_server_processes_on_one_home_both_answer_without_colliding_call_ids(tmp_path: Path):
    home = write_home(tmp_path)
    ids: list[str] = []

    async def main():
        async with connect(home) as one, connect(home) as two:
            for session in (one, two, one, two):
                header, text, is_error = await call(session, "executeCommand", sessionId=SID_A, command="$PID")
                assert not is_error, header
                ids.append(header["callId"])

    anyio.run(main)
    assert ids == ["0001", "0002", "0003", "0004"]


def test_get_session_id_mints_a_session_and_reports_the_server(tmp_path: Path):
    home = write_home(tmp_path)

    async def main():
        async with connect(home) as session:
            header, text, is_error = await call(session, "getSessionId")
            assert not is_error and text is None
            return header

    header = anyio.run(main)
    assert is_valid_session_id(header["sessionId"])
    assert header["serverVersion"] == __version__
    assert Path(header["serverRoot"]) == SRC
    transcript = Path(header["transcript"])
    assert transcript == home / "sessions" / f"{header['sessionId']}.txt"
    assert " | getSessionId | " in transcript.read_text(encoding="utf-8").splitlines()[0]


def test_get_session_creates_an_unknown_valid_session_and_lists_promoted_jobs(tmp_path: Path):
    home = write_home(tmp_path)

    async def main():
        async with connect(home) as session:
            header, _, _ = await call(session, "getSession", sessionId=SID_A)
            assert header == {"sessionId": SID_A, "cwd": str(tmp_path), "envSet": [], "envRemoved": [],
                              "foreground": None, "jobs": []}
            promoted, _, _ = await call(session, "executeCommand", sessionId=SID_A, command="Start-Sleep 3",
                                        timeoutSeconds=0)
            await call(session, "executeCommand", sessionId=SID_A, command="$env:MCP_T = 'бар'")
            header, _, _ = await call(session, "getSession", sessionId=SID_A)
            assert header["envSet"] == ["MCP_T"]
            assert [j["jobId"] for j in header["jobs"]] == [promoted["jobId"]]
            assert header["jobs"][0]["state"] == "running" and header["jobs"][0]["exitCode"] is None
            await anyio.sleep(5)
            header, _, _ = await call(session, "getSession", sessionId=SID_A)
            assert header["jobs"][0]["state"] == "done" and header["jobs"][0]["exitCode"] == 0

    anyio.run(main)
    assert (home / "state" / f"{SID_A}.json").is_file()


def test_session_state_persists_through_the_server(tmp_path: Path):
    (tmp_path / "sub").mkdir()

    async def main():
        async with connect(write_home(tmp_path)) as session:
            header, _, _ = await call(session, "executeCommand", sessionId=SID_A, command="Set-Location sub")
            assert header["cwd"] == str(tmp_path / "sub")
            _, text, _ = await call(session, "executeCommand", sessionId=SID_A, command="$PWD.ProviderPath")
            assert text == str(tmp_path / "sub")
            # a cwd given for one call is not persisted
            header, text, _ = await call(session, "executeCommand", sessionId=SID_A, command="$PWD.ProviderPath",
                                         cwd=str(tmp_path))
            assert text == str(tmp_path) and header["cwd"] == str(tmp_path / "sub")
            # the server's own PYTHONPATH never reaches a command; the session id does
            _, text, _ = await call(session, "executeCommand", sessionId=SID_A,
                                    command="\"[$env:PYTHONPATH][$env:MH_MCP_SESSION_ID]\"")
            assert text == f"[][{SID_A}]"

    anyio.run(main)


def test_refusals_carry_their_kind_in_content_0(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            assert await call(session, "executeCommand", sessionId="20261322-000000-aaaaaa", command="'x'") == \
                ({"error": "BAD_SESSION_ID"}, None, True)
            assert await call(session, "executeCommand", sessionId=SID_A, command="'x'", timeoutSeconds=100) == \
                ({"error": "TIMEOUT_TOO_LARGE", "max": 58}, None, True)
            header, _, is_error = await call(session, "executeCommand", sessionId=SID_A, command="'x'",
                                             cwd="C:/Windows")
            assert is_error and header["error"] == "CWD_OUTSIDE_ROOTS"
            header, _, is_error = await call(session, "executeCommand", sessionId=SID_A)
            assert is_error and header["error"] == "BAD_ARGUMENTS" and "command" in header["message"]

    anyio.run(main)


def test_a_failing_command_reports_its_exit_code_and_output(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            header, text, is_error = await call(session, "executeCommand", sessionId=SID_A,
                                                command="'out'; cmd /c exit 9")
            assert not is_error
            assert header["exitCode"] == 9 and text == "out" and header["lines"] == 1

    anyio.run(main)
