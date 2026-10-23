import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from mh_mcp_server.calls import read_pids, write_call
from mh_mcp_server.processes import process_alive
from support import PWSH, SHELL_ARGS, SRC, new_call_dir, output_of, start_argv, start_ps1, wait_exit


def test_a_script_ending_exit_7_yields_exit_7(tmp_path: Path):
    call_dir = start_ps1(tmp_path, "'before'\nexit 7\n")
    record = wait_exit(call_dir)
    assert record["exit"] == 7
    assert "before" in output_of(call_dir)
    assert record["seconds"] >= 0 and datetime.fromisoformat(record["endedAt"]).tzinfo is not None


def test_a_detached_grandchild_does_not_delay_exit_json(tmp_path: Path):
    # The grandchildren outlive the script by far more than the 3 s bound; the -NoNewWindow one inherits the
    # output.log handle. The test waits for them to end, so no handle outlives the test and blocks the next
    # session's cleanup of --basetemp (nothing is killed).
    script = (
        "$a = Start-Process -PassThru -NoNewWindow -FilePath pwsh "
        "-ArgumentList '-NoLogo','-NoProfile','-NonInteractive','-Command','Start-Sleep 8'\n"
        "$b = Start-Process -PassThru -WindowStyle Hidden -FilePath pwsh "
        "-ArgumentList '-NoLogo','-NoProfile','-NonInteractive','-Command','Start-Sleep 8'\n"
        "'GPID=' + $a.Id\n"
        "'GPID=' + $b.Id\n"
        "'END=' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()\n"
    )
    started = time.monotonic()
    call_dir = start_ps1(tmp_path, script)
    record = wait_exit(call_dir, seconds=30)
    out = output_of(call_dir)
    grandchildren = [int(pid) for pid in re.findall(r"GPID=(\d+)", out)]
    assert len(grandchildren) == 2
    assert all(process_alive(pid, None) for pid in grandchildren)
    script_end_ms = int(re.search(r"END=(\d+)", out).group(1))
    ended_ms = datetime.fromisoformat(record["endedAt"]).timestamp() * 1000
    assert ended_ms - script_end_ms < 3000
    assert record["exit"] == 0
    deadline = time.monotonic() + 30
    while any(process_alive(pid, None) for pid in grandchildren) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not any(process_alive(pid, None) for pid in grandchildren)
    assert time.monotonic() - started < 30


HELPER = r"""
import os, sys
from pathlib import Path
from mh_mcp_server.processes import spawn_detached
from mh_mcp_server.calls import supervisor_argv
call_dir = Path(sys.argv[1])
spawn_detached(supervisor_argv(call_dir), dict(os.environ), call_dir.parent)
"""


def test_a_supervisor_spawned_from_a_helper_that_exits_at_once_still_writes_exit_json(tmp_path: Path):
    _, call_dir = new_call_dir(tmp_path)
    ps1 = call_dir / "command.ps1"
    ps1.write_text("Start-Sleep 2\n'survived'\n", encoding="utf-8")
    # call.json written by the test, the spawn done by a helper process that exits right after it
    write_call(call_dir, [PWSH, *SHELL_ARGS, str(ps1)], tmp_path, datetime.now().astimezone())
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    helper = subprocess.run([sys.executable, "-c", HELPER, str(call_dir)], env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=60)
    assert helper.returncode == 0, helper.stderr
    assert (call_dir / "exit.json").exists() is False
    record = wait_exit(call_dir)
    assert record["exit"] == 0
    assert "survived" in output_of(call_dir)


def test_output_of_pwsh_of_a_native_child_and_of_stderr_lands_in_output_log(tmp_path: Path):
    call_dir = start_ps1(tmp_path, "'from-pwsh'\ncmd /c echo from-native\n[Console]::Error.WriteLine('from-stderr')\n")
    assert wait_exit(call_dir)["exit"] == 0
    out = output_of(call_dir)
    assert "from-pwsh" in out and "from-native" in out and "from-stderr" in out


def test_pid_json_names_the_supervisor_and_the_shell(tmp_path: Path):
    call_dir = start_ps1(tmp_path, "Start-Sleep 3\n")
    deadline = time.monotonic() + 30
    pids = read_pids(call_dir)
    while pids is None and time.monotonic() < deadline:
        time.sleep(0.05)
        pids = read_pids(call_dir)
    assert pids is not None
    assert set(pids) == {"supervisorPid", "supervisorCreated", "shellPid", "shellCreated"}
    assert all(isinstance(v, int) and v > 0 for v in pids.values())
    assert pids["supervisorPid"] != pids["shellPid"]
    assert process_alive(pids["supervisorPid"], pids["supervisorCreated"])
    assert process_alive(pids["shellPid"], pids["shellCreated"])
    wait_exit(call_dir)
    time.sleep(0.5)
    assert not process_alive(pids["shellPid"], pids["shellCreated"])


def test_a_command_that_cannot_start_yields_127(tmp_path: Path):
    _, call_dir = new_call_dir(tmp_path)
    start_argv(call_dir, ["C:/sandbox/_work/temp/__absent__/none.exe"], tmp_path)
    assert wait_exit(call_dir)["exit"] == 127
    assert "cannot start" in output_of(call_dir)
    assert read_pids(call_dir) is None
