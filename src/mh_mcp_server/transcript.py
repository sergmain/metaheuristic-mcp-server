"""The transcript of a session: one block per tool call in `sessions/<sessionId>.txt` (plan 051, decision 14).

UTF-8 without BOM, LF line endings, append-only. Appends of one session are serialized inside a process by an
asyncio lock per session, and across processes by a lock on the file itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import anyio.to_thread

SESSIONS_DIR = "sessions"
_LOCK_DEADLINE_SECONDS = 60.0

# loop -> sessionId -> lock; an asyncio.Lock belongs to the loop it is first used on
_session_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = \
    weakref.WeakKeyDictionary()


def transcript_path(home: Path, session_id: str) -> Path:
    return home / SESSIONS_DIR / f"{session_id}.txt"


def format_block(at: datetime, call_id: str | None, tool: str, request: Mapping, response: Mapping | str,
                 seconds: float, text: str | None = None) -> str:
    """One transcript block.

    `request` and a Mapping `response` are written as JSON (indent 2, non-ASCII kept); a str `response` is written
    as it is. `text` is the raw text content of the response (content 1), written after it when non-empty. Every
    line ends with LF.
    """
    head = [at.isoformat(timespec="milliseconds")]
    if call_id is not None:
        head.append(f"call {call_id}")
    head.append(tool)
    head.append(f"{seconds:.2f} s")
    parts = [
        "=== " + " | ".join(head),
        "--- request",
        _json(request),
        "--- response",
        response if isinstance(response, str) else _json(response),
    ]
    if text:
        parts.append(text)
    block = "\n".join(parts)
    block = block.replace("\r\n", "\n")
    return block if block.endswith("\n") else block + "\n"


async def append(home: Path, session_id: str, block: str) -> None:
    """Appends `block` to the session's transcript; concurrent appends of one session never interleave."""
    path = transcript_path(home, session_id)
    data = block.encode("utf-8")
    async with _lock_for(session_id):
        await anyio.to_thread.run_sync(append_blocking, path, data)


def append_blocking(path: Path, data: bytes) -> None:
    """The blocking part of `append`: the whole of `data` written at the end of `path` under a file lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0))
    try:
        _lock_file(fd)
        try:
            os.lseek(fd, 0, os.SEEK_END)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        finally:
            _unlock_file(fd)
    finally:
        os.close(fd)


def _lock_for(session_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _session_locks.setdefault(loop, {})
    lock = locks.get(session_id)
    if lock is None:
        lock = locks[session_id] = asyncio.Lock()
    return lock


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


if os.name == "nt":
    import msvcrt

    def _lock_file(fd: int) -> None:
        # byte 0 is the lock token; msvcrt.locking works from the current position. LK_NBLCK in a short-sleep loop,
        # not LK_LOCK, which retries only once a second.
        deadline = time.monotonic() + _LOCK_DEADLINE_SECONDS
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.005)

    def _unlock_file(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock_file(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
