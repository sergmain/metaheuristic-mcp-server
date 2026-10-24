"""One log file per server instance: `logs/server-<sid>.log`, `<sid>` minted like a sessionId, newest LOG_FILES_KEPT kept."""

import os
import re
from pathlib import Path

import anyio
import pytest

from mh_mcp_server import __version__
from mh_mcp_server.__main__ import LOG_FILES_KEPT, instance_log_name, prune_logs
from mh_mcp_server.session_id import is_valid_session_id
from support import connect_plain, write_home

LOG_NAME_RE = re.compile(r"^server-(?P<sid>.+)\.log$")


def touch_logs(logs: Path, sids: list[str]) -> None:
    logs.mkdir(parents=True, exist_ok=True)
    for sid in sids:
        (logs / instance_log_name(sid)).write_text(f"{sid}\n", encoding="utf-8")


def remaining(logs: Path) -> list[str]:
    return sorted(p.name for p in logs.iterdir())


def test_the_instance_log_name_is_the_sid_inside_server_dot_log():
    assert instance_log_name("20261023-170000-0c1e17") == "server-20261023-170000-0c1e17.log"


def test_prune_keeps_the_newest_instance_logs_by_their_sid(tmp_path: Path):
    logs = tmp_path / "logs"
    touch_logs(logs, ["20261023-170004-000004", "20261021-235959-ffffff", "20261023-170000-000001",
                      "20261023-170003-000003", "20261023-170002-000002"])
    prune_logs(logs, 2)
    assert remaining(logs) == ["server-20261023-170003-000003.log", "server-20261023-170004-000004.log"]


def test_prune_leaves_files_that_are_not_instance_logs(tmp_path: Path):
    logs = tmp_path / "logs"
    touch_logs(logs, ["20261023-170000-000001", "20261023-170001-000002"])
    (logs / "server.log").write_text("the pre-0.1.7 shared log\n", encoding="utf-8")
    (logs / "notes.txt").write_text("x\n", encoding="utf-8")
    prune_logs(logs, 1)
    assert remaining(logs) == ["notes.txt", "server-20261023-170001-000002.log", "server.log"]


def test_prune_removes_nothing_when_there_are_no_more_than_keep(tmp_path: Path):
    logs = tmp_path / "logs"
    touch_logs(logs, ["20261023-170000-000001", "20261023-170001-000002"])
    prune_logs(logs, 2)
    assert remaining(logs) == ["server-20261023-170000-000001.log", "server-20261023-170001-000002.log"]
    prune_logs(logs, 5)
    assert len(remaining(logs)) == 2


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to delete a file another handle holds open")
def test_prune_skips_a_log_another_instance_still_holds_open(tmp_path: Path):
    logs = tmp_path / "logs"
    touch_logs(logs, ["20261023-170000-000001", "20261023-170001-000002", "20261023-170002-000003"])
    with open(logs / "server-20261023-170000-000001.log", "a", encoding="utf-8"):
        prune_logs(logs, 1)
        assert remaining(logs) == ["server-20261023-170000-000001.log", "server-20261023-170002-000003.log"]


def test_two_servers_side_by_side_on_one_home_write_one_log_each(tmp_path: Path):
    home = write_home(tmp_path)
    pids: list[int] = []

    async def main():
        async with connect_plain(home, tmp_path / "server1.err") as (server1, _), \
                connect_plain(home, tmp_path / "server2.err") as (server2, _):
            pids.extend([server1.pid, server2.pid])

    anyio.run(main)
    logs = sorted((home / "logs").glob("server-*.log"))
    assert len(logs) == 2, [p.name for p in logs]
    seen: dict[int, str] = {}
    for log in logs:
        sid = LOG_NAME_RE.fullmatch(log.name)["sid"]
        assert is_valid_session_id(sid), log.name
        text = log.read_text(encoding="utf-8")
        log_pids = {int(p) for p in re.findall(r"\[pid (\d+)\]", text)}
        assert len(log_pids) == 1, (log.name, text)
        assert f"starting {__version__}" in text, (log.name, text)
        seen[log_pids.pop()] = log.name
    assert sorted(seen) == sorted(pids)


def test_a_server_start_prunes_the_logs_to_the_newest_kept_including_its_own(tmp_path: Path):
    home = write_home(tmp_path)
    old = [f"20261022-1000{n:02d}-0000{n:02d}" for n in range(LOG_FILES_KEPT + 3)]
    touch_logs(home / "logs", old)

    async def main():
        async with connect_plain(home, tmp_path / "server.err"):
            pass

    anyio.run(main)
    names = remaining(home / "logs")
    assert len(names) == LOG_FILES_KEPT, names
    assert names[:-1] == [instance_log_name(sid) for sid in old[-(LOG_FILES_KEPT - 1):]]
    assert names[-1] > instance_log_name(old[-1])   # this instance's own file, minted after the old ones
