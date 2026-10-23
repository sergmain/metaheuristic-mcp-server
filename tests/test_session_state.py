import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from mh_mcp_server.calls import runs_dir
from mh_mcp_server.processes import process_alive, process_created
from mh_mcp_server.session_state import (SessionState, acquire_foreground, apply_state_out, foreground_holder,
                                         load_state, lock_path, release_foreground, resolve_cwd, save_state,
                                         update_foreground)

SID = "20261023-154207-0a1b2c"
ROOTS = (Path("C:/sandbox/_work"),)
NOT_PERSISTED = frozenset({"MH_MCP_SESSION_ID", "MH_MCP_CALL_ID", "PGPASSWORD"})
ME = (os.getpid(), process_created(os.getpid()))


def dead_process() -> tuple[int, int]:
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    created = process_created(proc.pid)
    proc.stdin.close()
    proc.wait(timeout=30)
    return proc.pid, created


def lock_holder(home: Path) -> dict:
    return json.loads(lock_path(home, SID).read_text(encoding="utf-8"))


def test_a_second_acquire_returns_the_first_call_id(tmp_path: Path):
    assert acquire_foreground(tmp_path, SID, "0001", ME, ME, process_alive) is None
    assert acquire_foreground(tmp_path, SID, "0002", ME, ME, process_alive) == "0001"
    assert lock_holder(tmp_path)["callId"] == "0001"


def test_a_lock_whose_supervisor_is_dead_is_taken_over(tmp_path: Path):
    assert acquire_foreground(tmp_path, SID, "0001", dead_process(), ME, process_alive) is None
    assert acquire_foreground(tmp_path, SID, "0002", ME, ME, process_alive) is None
    assert lock_holder(tmp_path)["callId"] == "0002"


def test_a_lock_whose_server_is_dead_is_taken_over(tmp_path: Path):
    assert acquire_foreground(tmp_path, SID, "0001", ME, dead_process(), process_alive) is None
    assert acquire_foreground(tmp_path, SID, "0002", ME, ME, process_alive) is None
    assert lock_holder(tmp_path)["callId"] == "0002"


def test_a_lock_whose_call_has_exit_json_is_taken_over(tmp_path: Path):
    assert acquire_foreground(tmp_path, SID, "0001", ME, ME, process_alive) is None
    call_dir = runs_dir(tmp_path, SID) / "0001"
    call_dir.mkdir(parents=True)
    (call_dir / "exit.json").write_text('{"exit": 0}', encoding="utf-8")
    assert acquire_foreground(tmp_path, SID, "0002", ME, ME, process_alive) is None


def test_a_lock_without_a_supervisor_yet_is_busy_while_its_server_lives(tmp_path: Path):
    assert acquire_foreground(tmp_path, SID, "0001", None, ME, process_alive) is None
    assert acquire_foreground(tmp_path, SID, "0002", ME, ME, process_alive) == "0001"
    update_foreground(tmp_path, SID, "0001", ME, ME)
    assert lock_holder(tmp_path)["supervisorPid"] == os.getpid()
    assert foreground_holder(tmp_path, SID, process_alive) == "0001"


def test_release_removes_only_its_own_lock(tmp_path: Path):
    assert acquire_foreground(tmp_path, SID, "0001", ME, ME, process_alive) is None
    release_foreground(tmp_path, SID, "0002")
    assert foreground_holder(tmp_path, SID, process_alive) == "0001"
    release_foreground(tmp_path, SID, "0001")
    assert not lock_path(tmp_path, SID).exists()
    assert foreground_holder(tmp_path, SID, process_alive) is None
    assert acquire_foreground(tmp_path, SID, "0003", ME, ME, process_alive) is None


def test_racing_acquirers_get_exactly_one_lock(tmp_path: Path):
    results: dict[str, str | None] = {}
    barrier = threading.Barrier(8)

    def worker(call_id: str):
        barrier.wait()
        results[call_id] = acquire_foreground(tmp_path, SID, call_id, ME, ME, process_alive)

    threads = [threading.Thread(target=worker, args=(f"{n:04d}",)) for n in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [call_id for call_id, holder in results.items() if holder is None]
    assert len(winners) == 1
    assert all(holder == winners[0] for call_id, holder in results.items() if holder is not None)
    assert lock_holder(tmp_path)["callId"] == winners[0]


def test_racing_takeovers_of_a_stale_lock_get_exactly_one_lock(tmp_path: Path):
    assert acquire_foreground(tmp_path, SID, "0001", dead_process(), ME, process_alive) is None
    results: dict[str, str | None] = {}
    barrier = threading.Barrier(8)

    def worker(call_id: str):
        barrier.wait()
        results[call_id] = acquire_foreground(tmp_path, SID, call_id, ME, ME, process_alive)

    threads = [threading.Thread(target=worker, args=(f"{n:04d}",)) for n in range(2, 10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [call_id for call_id, holder in results.items() if holder is None]
    assert len(winners) == 1
    assert lock_holder(tmp_path)["callId"] == winners[0]


def test_load_state_of_a_new_session_is_the_default_cwd(tmp_path: Path):
    assert load_state(tmp_path, SID, Path("C:/sandbox/_work")) == SessionState(cwd=Path("C:/sandbox/_work"))


def test_save_and_load_round_trip_with_cyrillic(tmp_path: Path):
    state = SessionState(cwd=Path("C:/sandbox/_work/temp"), env_set={"MCP_T": "бар"},
                         env_removed=frozenset({"MCP_GONE"}))
    save_state(tmp_path, SID, state)
    loaded = load_state(tmp_path, SID, Path("C:/x"))
    assert loaded.cwd == state.cwd
    assert dict(loaded.env_set) == {"MCP_T": "бар"}
    assert loaded.env_removed == frozenset({"MCP_GONE"})
    assert "бар" in (tmp_path / "state" / f"{SID}.json").read_text(encoding="utf-8")


def test_apply_state_out_persists_a_cwd_under_the_roots():
    state, notes = apply_state_out(SessionState(cwd=Path("C:/sandbox/_work")),
                                   {"cwd": "C:\\sandbox\\_work\\temp"}, ROOTS, NOT_PERSISTED)
    assert state.cwd == Path("C:/sandbox/_work/temp")
    assert notes == []


def test_apply_state_out_keeps_the_old_cwd_outside_the_roots():
    state, notes = apply_state_out(SessionState(cwd=Path("C:/sandbox/_work")), {"cwd": "C:\\Windows"},
                                   ROOTS, NOT_PERSISTED)
    assert state.cwd == Path("C:/sandbox/_work")
    assert notes == ["cwdNotPersisted"]


def test_apply_state_out_never_persists_the_fixed_names_or_secrets():
    state, _ = apply_state_out(SessionState(cwd=Path("C:/sandbox/_work")),
                               {"envSet": {"MH_MCP_CALL_ID": "0009", "pgpassword": "pw", "MCP_A": "1"},
                                "envRemoved": ["MH_MCP_SESSION_ID"]}, ROOTS, NOT_PERSISTED)
    assert dict(state.env_set) == {"MCP_A": "1"}
    assert state.env_removed == frozenset()


def test_apply_state_out_set_then_removed_and_removed_then_set():
    state = SessionState(cwd=Path("C:/sandbox/_work"), env_set={"MCP_A": "1"}, env_removed=frozenset({"MCP_B"}))
    state, _ = apply_state_out(state, {"envSet": {"MCP_B": "2"}, "envRemoved": ["mcp_a"]}, ROOTS, NOT_PERSISTED)
    assert dict(state.env_set) == {"MCP_B": "2"}
    assert state.env_removed == frozenset({"mcp_a"})


def test_apply_state_out_without_a_report_keeps_the_state():
    state = SessionState(cwd=Path("C:/sandbox/_work"), env_set={"MCP_A": "1"})
    assert apply_state_out(state, {}, ROOTS, NOT_PERSISTED) == (state, [])


def test_resolve_cwd_falls_back_to_the_default_when_the_directory_is_gone(tmp_path: Path):
    assert resolve_cwd(tmp_path, Path("C:/sandbox/_work")) == (tmp_path, [])
    assert resolve_cwd(tmp_path / "gone", Path("C:/sandbox/_work")) == (Path("C:/sandbox/_work"), ["cwdReset"])
