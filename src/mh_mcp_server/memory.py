"""getMemory (0.1.9): the box's physical memory and commit charge, read inside the server - no pwsh, no WMI.

The executor watches memory throughout development runs (plan 049 / 050: stop at 27 GB used or under 2 GB of free
commit). Until 0.1.9 every look was a fresh pwsh process running `Get-CimInstance Win32_OperatingSystem` through
executeCommand - a process start and a CIM query for a handful of numbers. One kernel32 call answers the same numbers:

  GlobalMemoryStatusEx    ullTotalPhys / ullAvailPhys          physical memory, total and available - WMI's
                                                               TotalVisibleMemorySize and FreePhysicalMemory
                          ullTotalPageFile / ullAvailPageFile  the commit limit and what is left of it - WMI's
                                                               TotalVirtualMemorySize and FreeVirtualMemory
                          dwMemoryLoad                         the percent of physical memory in use

Every figure is GiB (2^30 bytes) with one decimal - the unit the scripts' `KB / 1MB` gave, so a threshold such as
"27 GB used" reads the same. memory_header is pure; read_status is the one boundary call. Off Windows read_status
raises OSError and the tool refuses with MEMORY_UNAVAILABLE.
"""

import ctypes
import sys
from dataclasses import dataclass

from mh_mcp_server.execution import Reply, refusal
from mh_mcp_server.session_id import is_valid_session_id

GIB = 1 << 30


@dataclass(frozen=True)
class MemoryStatus:
    """The numbers of one GlobalMemoryStatusEx call, in bytes (load in percent)."""
    total_phys: int
    avail_phys: int
    commit_limit: int
    commit_avail: int
    load: int


def gib(n: int) -> float:
    """Bytes as GiB, one decimal."""
    return round(n / GIB, 1)


def memory_header(status: MemoryStatus) -> dict:
    """The tool's JSON header of one reading: used = total - available, commit used = limit - available commit."""
    return {"usedGB": gib(status.total_phys - status.avail_phys), "availableGB": gib(status.avail_phys),
            "totalGB": gib(status.total_phys), "memoryLoad": status.load,
            "commitUsedGB": gib(status.commit_limit - status.commit_avail), "commitFreeGB": gib(status.commit_avail),
            "commitLimitGB": gib(status.commit_limit)}


class _MemoryStatusEx(ctypes.Structure):
    # MEMORYSTATUSEX (sysinfoapi.h); DWORD is 32-bit on Windows, which c_ulong is there
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def read_status() -> MemoryStatus:
    """One GlobalMemoryStatusEx call - an OSError off Windows or when the call fails."""
    if sys.platform != "win32":
        raise OSError("getMemory reads GlobalMemoryStatusEx, which exists on Windows only")
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError()
    return MemoryStatus(total_phys=status.ullTotalPhys, avail_phys=status.ullAvailPhys,
                        commit_limit=status.ullTotalPageFile, commit_avail=status.ullAvailPageFile,
                        load=status.dwMemoryLoad)


def get_memory(session_id: str) -> Reply:
    """The tool: one reading as the JSON header, recorded in the session's transcript like any call."""
    if not is_valid_session_id(session_id):
        return refusal("BAD_SESSION_ID")
    try:
        status = read_status()
    except OSError as e:
        return refusal("MEMORY_UNAVAILABLE", session_id, message=str(e))
    return Reply(memory_header(status), session_id=session_id)
