"""Detached spawning and process liveness (plan 051, decision 7, Phase 5).

Standard library only: `supervisor.py` imports this module from its own directory.

❗ `os.kill(pid, 0)` is never used on Windows: there it terminates the process. Liveness is OpenProcess +
GetExitCodeProcess, plus the process creation time, so a reused pid reads as dead.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping, Sequence


def spawn_detached(args: Sequence[str], env: Mapping[str, str], cwd: Path) -> int:
    """Starts a process that outlives this one and holds none of its console handles; its pid.

    The `spawn_detached` of metaheuristic-assets/dev-tools/surefire/run_test.py (L260-L271), plus env and cwd.
    """
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                  env=dict(env), cwd=str(cwd))
    if os.name != "nt":
        return subprocess.Popen(list(args), start_new_session=True, **kwargs).pid
    base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        # leave the caller's job object, so a client that kills its job on exit doesn't take the command with it
        return subprocess.Popen(list(args), creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs).pid
    except OSError:
        # the job forbids breakaway
        return subprocess.Popen(list(args), creationflags=base, **kwargs).pid


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME))
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def _creation_of(handle) -> int | None:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not _k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel),
                                    ctypes.byref(user)):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime

    def process_created(pid: int) -> int | None:
        """The process's creation time (100 ns units since 1601), None when the process cannot be opened."""
        if not pid:
            return None
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            return _creation_of(handle)
        finally:
            _k32.CloseHandle(handle)

    def process_alive(pid: int, created: int | None) -> bool:
        """Whether `pid` is running and is still the process created at `created` (None: creation not compared).

        A process that cannot be opened for lack of rights counts as alive - it exists, and declaring a live call
        lost would be worse than a late verdict.
        """
        if not pid:
            return False
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
                return False
            return created is None or _creation_of(handle) == created
        finally:
            _k32.CloseHandle(handle)
else:
    def process_created(pid: int) -> int | None:
        return 0 if process_alive(pid, None) else None

    def process_alive(pid: int, created: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)  # a probe on POSIX only
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
