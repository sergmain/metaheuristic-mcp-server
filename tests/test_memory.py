"""getMemory (0.1.9): the header is arithmetic over one reading; the reading is the real box's (Windows only)."""

import sys

import pytest

from mh_mcp_server import memory
from mh_mcp_server.memory import GIB, MemoryStatus, memory_header

SID = "20261026-153000-00000c"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="GlobalMemoryStatusEx is Windows only")


def test_the_header_is_used_available_total_and_commit_in_gib():
    status = MemoryStatus(total_phys=32 * GIB, avail_phys=11 * GIB + GIB // 2, commit_limit=48 * GIB,
                          commit_avail=20 * GIB, load=64)

    assert memory_header(status) == {"usedGB": 20.5, "availableGB": 11.5, "totalGB": 32.0, "memoryLoad": 64,
                                     "commitUsedGB": 28.0, "commitFreeGB": 20.0, "commitLimitGB": 48.0}


def test_gib_keeps_one_decimal():
    assert memory.gib(0) == 0.0
    assert memory.gib(GIB) == 1.0
    assert memory.gib(27 * GIB + 3 * GIB // 10) == 27.3
    assert memory.gib(GIB // 1000) == 0.0


@windows_only
def test_read_status_reads_the_box():
    status = memory.read_status()

    assert status.total_phys > GIB, status
    assert 0 < status.avail_phys <= status.total_phys, status
    assert 0 < status.commit_avail <= status.commit_limit, status
    assert 0 <= status.load <= 100, status


@windows_only
def test_get_memory_answers_the_header_for_its_session():
    reply = memory.get_memory(SID)

    assert not reply.is_error, reply
    assert reply.session_id == SID
    assert sorted(reply.header) == ["availableGB", "commitFreeGB", "commitLimitGB", "commitUsedGB", "memoryLoad",
                                    "totalGB", "usedGB"]
    header = reply.header
    assert abs(header["usedGB"] + header["availableGB"] - header["totalGB"]) <= 0.2, header
    assert abs(header["commitUsedGB"] + header["commitFreeGB"] - header["commitLimitGB"]) <= 0.2, header


@pytest.mark.parametrize("bad", ["", "x", "20261026-153000-00000", "2026-10-26"])
def test_get_memory_refuses_a_bad_session_id(bad):
    reply = memory.get_memory(bad)

    assert reply.is_error
    assert reply.header == {"error": "BAD_SESSION_ID"}
