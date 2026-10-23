"""readFile, writeFile, editFile - exact file tools (plan 051, decision 21, Phase 9).

UTF-8; a BOM is kept when present and never added; CRLF and LF files keep their line endings byte for byte outside
the edited text; a file with mixed line endings is refused by editFile rather than normalized; every write is a temp
file in the same directory plus os.replace; an editFile call is all-or-nothing. Line counting is streamed in chunks,
so a large log is never loaded whole.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from mh_mcp_server.calls import RUNS_DIR
from mh_mcp_server.config import excluded_by, is_under
from mh_mcp_server.execution import Reply, Runtime, refusal
from mh_mcp_server.output import decode_line
from mh_mcp_server.session_id import is_valid_session_id
from mh_mcp_server.session_state import load_state

BOM = b"\xef\xbb\xbf"
CHUNK = 1 << 20
BINARY_PROBE = 8192
REPLACE_RETRIES = 3
REPLACE_RETRY_SECONDS = 0.1
OLD_PREVIEW_CHARS = 120


@dataclass(frozen=True)
class FileStats:
    size: int
    lines: int      # a last line without a line ending counts; an empty file has 0
    crlf: int
    lf: int         # bare LF (not preceded by CR)
    bom: bool
    binary: bool    # a NUL byte in the first 8 KB

    @property
    def line_ending(self) -> str:
        if not self.crlf and not self.lf:
            return "none"
        if not self.lf:
            return "crlf"
        return "lf" if not self.crlf else "mixed"


# ==================== pure / streaming ====================

def file_stats(path: Path) -> FileStats:
    """Line count, line endings, BOM and the binary probe of `path` in one streamed pass."""
    size = newlines = crlf = 0
    prev_cr = False
    last = b""
    with open(path, "rb") as f:
        head = f.read(BINARY_PROBE)
        chunk = head
        while chunk:
            size += len(chunk)
            newlines += chunk.count(b"\n")
            crlf += chunk.count(b"\r\n") + (1 if prev_cr and chunk[:1] == b"\n" else 0)
            prev_cr = chunk[-1:] == b"\r"
            last = chunk[-1:]
            chunk = f.read(CHUNK)
    lines = newlines + (1 if size and last != b"\n" else 0)
    return FileStats(size, lines, crlf, newlines - crlf, head.startswith(BOM), b"\x00" in head)


def stats_of_bytes(data: bytes) -> FileStats:
    newlines = data.count(b"\n")
    crlf = data.count(b"\r\n")
    lines = newlines + (1 if data and not data.endswith(b"\n") else 0)
    return FileStats(len(data), lines, crlf, newlines - crlf, data.startswith(BOM), b"\x00" in data[:BINARY_PROBE])


def offset_of_line(path: Path, line: int) -> int:
    """The byte offset where 1-based `line` starts (the file's size when it has fewer lines)."""
    needed = line - 1
    offset = 0
    if needed <= 0:
        return 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                return offset
            found = chunk.count(b"\n")
            if found < needed:
                needed -= found
                offset += len(chunk)
                continue
            pos = -1
            for _ in range(needed):
                pos = chunk.index(b"\n", pos + 1)
            return offset + pos + 1


def read_numbered(path: Path, start_line: int, end_line: int, max_lines: int, max_bytes: int, bom: bool
                  ) -> tuple[list[str], int | None]:
    """`N: <line>` entries from `start_line` to at most `end_line`, stopping at `max_lines` / `max_bytes` on a line
    boundary; and the number of a first line that had to be truncated to fit, if any (it is always returned, so a
    read always makes progress)."""
    entries: list[str] = []
    used = 0
    truncated: int | None = None
    number = start_line
    with open(path, "rb") as f:
        f.seek(offset_of_line(path, start_line))
        for raw in f:
            if number > end_line or len(entries) >= max_lines:
                break
            text, _ = decode_line(raw)
            if number == 1 and bom and text.startswith("\ufeff"):
                text = text[1:]
            entry = f"{number}: {text}"
            size = len(entry.encode("utf-8")) + 1
            if used + size > max_bytes:
                if entries:
                    break
                entry = entry.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
                truncated = number
                size = max_bytes
            entries.append(entry)
            used += size
            number += 1
    return entries, truncated


def normalize_content(content: str, line_ending: str) -> str:
    text = content.replace("\r\n", "\n")
    return text.replace("\n", "\r\n") if line_ending == "crlf" else text


def apply_edits(text: str, edits: Sequence[Mapping[str, Any]], eol: str) -> tuple[str, list[dict]] | dict:
    """`text` after every edit in order, and per edit {index, matches, line}; or the refusal of the first edit that
    does not apply. Each `old` must occur exactly `count` times in the text the previous edit left."""
    results: list[dict] = []
    for index, edit in enumerate(edits):
        old, new, count = edit.get("old"), edit.get("new"), edit.get("count", 1)
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            return {"error": "BAD_ARGUMENTS", "index": index, "message": "old must be a non-empty string, new a string"}
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            return {"error": "BAD_ARGUMENTS", "index": index, "message": "count must be an integer >= 1"}
        old_n, new_n = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        if "\r" in old_n or "\r" in new_n:
            return {"error": "LONE_CR", "index": index}
        if eol == "\r\n":
            old_n, new_n = old_n.replace("\n", "\r\n"), new_n.replace("\n", "\r\n")
        found = text.count(old_n)
        if found != count:
            return {"error": "MATCH_COUNT", "index": index, "expected": count, "found": found,
                    "old": old[:OLD_PREVIEW_CHARS]}
        position = text.find(old_n)
        results.append({"index": index, "matches": found, "line": text.count("\n", 0, position) + 1})
        text = text.replace(old_n, new_n)
    return text, results


def atomic_write(path: Path, data: bytes) -> None:
    """`data` into a temp file beside `path`, then `os.replace`, retried on PermissionError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.mcp-tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(REPLACE_RETRIES + 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(REPLACE_RETRY_SECONDS)


# ==================== tools ====================

def read_file(rt: Runtime, session_id: str, path: str, from_line: int = 1, to_line: int | None = None,
              max_lines: int | None = None) -> Reply:
    # the home's runs/ dir is readable too: every logPath this server hands out lies there, and a header that names a
    # log the caller cannot read is a dead end (the home itself is outside the roots, e.g. C:/_mh/mh-mcp-server)
    target = _target(rt, session_id, path, (*rt.config.read_roots, rt.home / RUNS_DIR))
    if isinstance(target, Reply):
        return target
    if from_line == 0 or (max_lines is not None and max_lines < 1) or (to_line is not None and to_line < 1):
        return refusal("BAD_ARGUMENTS", session_id, message="fromLine must not be 0; toLine and maxLines >= 1")
    if not target.is_file():
        return refusal("NOT_FOUND", session_id, path=str(target))
    stats = file_stats(target)
    if stats.binary:
        return refusal("BINARY_FILE", session_id, path=str(target))
    total = stats.lines
    start = from_line if from_line > 0 else max(total + from_line + 1, 1)
    end = total if to_line is None else min(to_line, total)
    limit = rt.config.read_max_lines if max_lines is None else min(max_lines, rt.config.read_max_lines)
    entries, truncated = ([], None) if start > end else \
        read_numbered(target, start, end, limit, rt.config.read_max_bytes, stats.bom)
    last = start + len(entries) - 1
    header: dict[str, Any] = {"path": str(target), "totalLines": total, "fromLine": start, "toLine": last}
    if last < end:
        header["nextFromLine"] = last + 1
    header["lineEnding"] = stats.line_ending
    header["bom"] = stats.bom
    if truncated is not None:
        header["truncatedLine"] = truncated
    return Reply(header, "\n".join(entries) or None, False, None, session_id)


def write_file(rt: Runtime, session_id: str, path: str, content: str, mode: str = "create",
               line_ending: str | None = None) -> Reply:
    target = _target(rt, session_id, path, rt.config.write_roots)
    if isinstance(target, Reply):
        return target
    if mode not in ("create", "overwrite") or line_ending not in (None, "lf", "crlf"):
        return refusal("BAD_ARGUMENTS", session_id, message='mode is "create" or "overwrite"; lineEnding "lf" or "crlf"')
    exists = target.exists()
    if exists and not target.is_file():
        return refusal("WRITE_FAILED", session_id, path=str(target), message="not a file")
    if exists and mode == "create":
        return refusal("EXISTS", session_id, path=str(target))
    bom = False
    ending = line_ending
    if exists:
        stats = file_stats(target)
        bom = stats.bom
        if ending is None:
            ending = "crlf" if stats.line_ending == "crlf" else "lf"
    ending = ending or "lf"
    text = normalize_content(content, ending)
    data = (BOM if bom and not text.startswith("\ufeff") else b"") + text.encode("utf-8")
    try:
        atomic_write(target, data)
    except OSError as e:
        return refusal("WRITE_FAILED", session_id, path=str(target), message=f"{type(e).__name__}: {e}")
    return Reply({"path": str(target), "bytes": len(data), "lines": stats_of_bytes(data).lines, "lineEnding": ending},
                 None, False, None, session_id)


def edit_file(rt: Runtime, session_id: str, path: str, edits: Sequence[Mapping[str, Any]],
              dry_run: bool = False) -> Reply:
    target = _target(rt, session_id, path, rt.config.write_roots)
    if isinstance(target, Reply):
        return target
    if not target.is_file():
        return refusal("NOT_FOUND", session_id, path=str(target))
    if not edits:
        return refusal("BAD_ARGUMENTS", session_id, message="edits must not be empty")
    data = target.read_bytes()
    bom = data.startswith(BOM)
    text = (data[3:] if bom else data).decode("utf-8", "surrogateescape")
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return refusal("MIXED_LINE_ENDINGS", session_id, crlf=crlf, lf=lf)
    applied = apply_edits(text, edits, "\r\n" if crlf else "\n")
    if isinstance(applied, dict):
        return Reply(applied, None, True, None, session_id)
    new_text, results = applied
    new_data = (BOM if bom else b"") + new_text.encode("utf-8", "surrogateescape")
    if not dry_run and new_data != data:
        try:
            atomic_write(target, new_data)
        except OSError as e:
            return refusal("WRITE_FAILED", session_id, path=str(target), message=f"{type(e).__name__}: {e}")
    header: dict[str, Any] = {"path": str(target), "edits": results, "bytes": len(new_data)}
    if dry_run:
        header["dryRun"] = True
    return Reply(header, None, False, None, session_id)


def _target(rt: Runtime, session_id: str, path: str, roots: Sequence[Path]) -> Path | Reply:
    if not is_valid_session_id(session_id):
        return refusal("BAD_SESSION_ID")
    state = load_state(rt.home, session_id, rt.config.default_cwd)
    target = (state.cwd / path).resolve()
    excluded = excluded_by(target, rt.config.excluded_paths)
    if excluded is not None:
        return refusal("PATH_EXCLUDED", session_id, path=str(target), excluded=excluded)
    if not is_under(target, roots):
        return refusal("PATH_OUTSIDE_ROOTS", session_id, path=str(target))
    return target
