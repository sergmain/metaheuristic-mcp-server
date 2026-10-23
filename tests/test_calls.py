import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from mh_mcp_server.calls import (STARTUP_GRACE_SECONDS, allocate_call, call_state, read_exit, write_call)
from mh_mcp_server.processes import process_alive, process_created


def test_8_threads_by_25_allocations_give_200_distinct_ids(tmp_path: Path):
    runs = tmp_path / "runs" / "20261023-154207-0a1b2c"
    results: list[tuple[str, Path]] = []
    guard = threading.Lock()

    def worker():
        for _ in range(25):
            allocated = allocate_call(runs)
            with guard:
                results.append(allocated)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [call_id for call_id, _ in results]
    assert len(set(ids)) == 200
    assert sorted(ids) == [f"{n:04d}" for n in range(1, 201)]
    assert all(call_dir.is_dir() and call_dir.name == call_id for call_id, call_dir in results)


def test_allocation_continues_after_the_highest_existing_id(tmp_path: Path):
    runs = tmp_path / "runs"
    (runs / "0007").mkdir(parents=True)
    (runs / "notes").mkdir()
    assert allocate_call(runs)[0] == "0008"


def dead_process() -> tuple[int, int]:
    """The pid and creation time of a real process that has already exited."""
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    created = process_created(proc.pid)
    proc.stdin.close()
    proc.wait(timeout=30)
    assert created is not None
    return proc.pid, created


def write_pids(call_dir: Path, pid: int, created: int) -> None:
    (call_dir / "pid.json").write_text(json.dumps({"supervisorPid": pid, "supervisorCreated": created,
                                                    "shellPid": pid, "shellCreated": created}), encoding="utf-8")


def test_a_dead_supervisor_without_exit_json_is_lost(tmp_path: Path):
    pid, created = dead_process()
    call_dir = tmp_path / "0001"
    call_dir.mkdir()
    write_call(call_dir, ["pwsh"], tmp_path, datetime.now().astimezone())
    write_pids(call_dir, pid, created)
    assert not process_alive(pid, created)
    assert call_state(call_dir, process_alive) == "lost"


def test_a_live_supervisor_is_running_and_exit_json_makes_it_done(tmp_path: Path):
    call_dir = tmp_path / "0001"
    call_dir.mkdir()
    write_call(call_dir, ["pwsh"], tmp_path, datetime.now().astimezone())
    write_pids(call_dir, os.getpid(), process_created(os.getpid()))
    assert call_state(call_dir, process_alive) == "running"
    (call_dir / "exit.json").write_text('{"exit": 0, "endedAt": "2026-10-23T15:42:07.512+03:00", "seconds": 1.0}',
                                        encoding="utf-8")
    assert call_state(call_dir, process_alive) == "done"
    assert read_exit(call_dir) == {"exit": 0, "endedAt": "2026-10-23T15:42:07.512+03:00", "seconds": 1.0}


def test_a_reused_pid_reads_as_dead():
    created = process_created(os.getpid())
    assert process_alive(os.getpid(), created)
    assert not process_alive(os.getpid(), created + 1)


def test_without_pid_json_a_young_call_is_running_and_an_old_one_lost(tmp_path: Path):
    call_dir = tmp_path / "0001"
    call_dir.mkdir()
    created_at = datetime.now().astimezone()
    write_call(call_dir, ["pwsh"], tmp_path, created_at)
    at = created_at.timestamp()
    assert call_state(call_dir, process_alive, now=lambda: at + 1) == "running"
    assert call_state(call_dir, process_alive, now=lambda: at + STARTUP_GRACE_SECONDS + 1) == "lost"


def test_read_exit_is_none_without_the_file(tmp_path: Path):
    assert read_exit(tmp_path) is None


def test_call_json_holds_argv_cwd_and_created_at_but_no_environment(tmp_path: Path):
    created_at = datetime(2026, 10, 23, 15, 42, 7, 512000).astimezone()
    write_call(tmp_path, ["pwsh", "-File", "c.ps1"], Path("C:/sandbox/_work"), created_at)
    record = json.loads((tmp_path / "call.json").read_text(encoding="utf-8"))
    assert record == {"argv": ["pwsh", "-File", "c.ps1"], "cwd": str(Path("C:/sandbox/_work")),
                      "createdAt": created_at.isoformat(timespec="milliseconds")}
    assert datetime.fromisoformat(record["createdAt"]) - created_at == timedelta(0)
