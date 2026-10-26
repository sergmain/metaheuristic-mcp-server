"""searchFiles, listFiles, fileInfo (0.1.10): the most frequent pwsh commands of the chats, answered in-process.

mh-mcp's own transcripts of 2026-10-23 - 10-26 (3,721 calls; temp/mh-mcp-usage.py): 818 commands grepped (Select-String,
161 of them over a whole tree), 380 listed a directory (211 a recursive find), 162 counted a file's lines or bytes - each
one a fresh pwsh process (1.3 - 3.4 s on average) for what is a walk over a few files.

Every path obeys readFile's rules: the session's cwd resolves a relative root, the root must lie under the read roots or the
home's runs/ dir, an [excluded] root is refused and an [excluded] entry under the root is skipped. A walk never follows a
symbolic link or a junction, and skips SKIP_DIRS unless includeSkipped. Output is bounded: maxResults entries and the
readFile byte budget, `truncated` in the header when either stopped it.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from mh_mcp_server.calls import RUNS_DIR
from mh_mcp_server.config import excluded_by
from mh_mcp_server.execution import Reply, Runtime, refusal
from mh_mcp_server.files import BINARY_PROBE, _target, file_stats

SKIP_DIRS = frozenset({".git", "target", "node_modules", "__pycache__", ".idea", ".venv", "dist", ".pytest_cache"})
SEARCH_RESULTS_DEFAULT = 200
LIST_RESULTS_DEFAULT = 500
RESULTS_MAX = 5000
SEARCH_DEPTH = 64
LINE_CHARS = 300
LARGE_FILE_BYTES = 64 << 20
INFO_PATHS_MAX = 50


# ==================== pure ====================

def globs_of(glob: str | None) -> list[str]:
    """'*.java, *.py' -> ['*.java', '*.py']; None or blank -> []."""
    return [g.strip() for g in (glob or "").split(",") if g.strip()]


def name_matches(name: str, globs: Sequence[str]) -> bool:
    """True when there is no glob or the name matches one (case-insensitive on Windows, as fnmatch is)."""
    return not globs or any(fnmatch.fnmatch(name, g) for g in globs)


def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix() if path != root else path.name


def clip(text: str) -> str:
    text = text.rstrip("\r\n")
    return text if len(text) <= LINE_CHARS else text[:LINE_CHARS] + "..."


def stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")


# ==================== the walk ====================

def walk(root: Path, max_depth: int, excluded: Sequence[str], include_skipped: bool) -> Iterator[tuple[Path, bool, int]]:
    """(path, is_dir, depth) of every entry under `root` down to `max_depth` (1 = the root's own entries), by name, depth
    first; links and junctions are neither followed nor listed; SKIP_DIRS are listed but not entered."""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
        except OSError:
            continue
        below: list[tuple[Path, int]] = []
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink() or entry.is_junction() or excluded_by(path, excluded) is not None:
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            yield path, is_dir, depth + 1
            if is_dir and depth + 1 < max_depth and (include_skipped or entry.name not in SKIP_DIRS):
                below.append((path, depth + 1))
        stack.extend(reversed(below))


def _root(rt: Runtime, session_id: str, root: str) -> Path | Reply:
    target = _target(rt, session_id, root, (*rt.config.read_roots, rt.home / RUNS_DIR))
    if isinstance(target, Path) and not target.exists():
        return refusal("NOT_FOUND", session_id, path=str(target))
    return target


def _limit(max_results: int | None, default: int) -> int | None:
    if max_results is None:
        return default
    return max_results if 1 <= max_results <= RESULTS_MAX else None


# ==================== searchFiles ====================

def search_files(rt: Runtime, session_id: str, root: str, regex: str, glob: str | None = None,
                 max_results: int | None = None, context: int = 0, ignore_case: bool = False,
                 include_skipped: bool = False) -> Reply:
    started = time.monotonic()
    target = _root(rt, session_id, root)
    if isinstance(target, Reply):
        return target
    limit = _limit(max_results, SEARCH_RESULTS_DEFAULT)
    if limit is None or not 0 <= context <= 10:
        return refusal("BAD_ARGUMENTS", session_id, message=f"maxResults 1 - {RESULTS_MAX}, context 0 - 10")
    try:
        pattern = re.compile(regex, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return refusal("BAD_ARGUMENTS", session_id, message=f"regex: {e}")
    globs = globs_of(glob)
    base = target if target.is_dir() else target.parent
    candidates = [target] if target.is_file() else \
        (p for p, is_dir, _ in walk(target, SEARCH_DEPTH, rt.config.excluded_paths, include_skipped)
         if not is_dir and name_matches(p.name, globs))
    out: list[str] = []
    used = matches = scanned = with_matches = binary = large = 0
    truncated = False
    for path in candidates:
        try:
            size = path.stat().st_size
            if size > LARGE_FILE_BYTES:
                large += 1
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:BINARY_PROBE]:
            binary += 1
            continue
        scanned += 1
        lines = data.decode("utf-8", "replace").splitlines()
        name = rel(path, base)
        hits = [i for i, line in enumerate(lines) if pattern.search(line)]
        if not hits:
            continue
        with_matches += 1
        shown: set[int] = set()
        for i in hits:
            if matches >= limit:
                truncated = True
                break
            matches += 1
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                if j in shown:
                    continue
                shown.add(j)
                line = f"{name}:{j + 1}{':' if j == i or j in hits else '-'} {clip(lines[j])}"
                used += len(line.encode("utf-8")) + 1
                if used > rt.config.read_max_bytes:
                    truncated = True
                    break
                out.append(line)
            if truncated:
                break
        if truncated:
            break
    header: dict[str, Any] = {"root": str(target), "matches": matches, "files": with_matches, "scanned": scanned,
                              "seconds": round(time.monotonic() - started, 2)}
    if binary:
        header["skippedBinary"] = binary
    if large:
        header["skippedLarge"] = large
    if truncated:
        header["truncated"] = True
    return Reply(header, "\n".join(out) or None, False, None, session_id)


# ==================== listFiles ====================

def list_files(rt: Runtime, session_id: str, root: str, glob: str | None = None, max_depth: int = 1,
               max_results: int | None = None, include_skipped: bool = False) -> Reply:
    target = _root(rt, session_id, root)
    if isinstance(target, Reply):
        return target
    limit = _limit(max_results, LIST_RESULTS_DEFAULT)
    if limit is None or not 1 <= max_depth <= SEARCH_DEPTH:
        return refusal("BAD_ARGUMENTS", session_id, message=f"maxResults 1 - {RESULTS_MAX}, maxDepth 1 - {SEARCH_DEPTH}")
    globs = globs_of(glob)
    out: list[str] = []
    dirs = files = 0
    truncated = False
    entries = [(target, False, 0)] if target.is_file() else \
        walk(target, max_depth, rt.config.excluded_paths, include_skipped)
    base = target if target.is_dir() else target.parent
    for path, is_dir, _ in entries:
        if is_dir and globs or not is_dir and not name_matches(path.name, globs):
            continue
        if dirs + files >= limit:
            truncated = True
            break
        try:
            st = path.stat()
        except OSError:
            continue
        if is_dir:
            dirs += 1
            out.append(f"d {'-':>12}  {stamp(st.st_mtime)}  {rel(path, base)}/")
        else:
            files += 1
            out.append(f"f {st.st_size:>12}  {stamp(st.st_mtime)}  {rel(path, base)}")
    header: dict[str, Any] = {"root": str(target), "dirs": dirs, "files": files}
    if truncated:
        header["truncated"] = True
    return Reply(header, "\n".join(out) or None, False, None, session_id)


# ==================== fileInfo ====================

def file_info(rt: Runtime, session_id: str, paths: Sequence[str]) -> Reply:
    if not paths or len(paths) > INFO_PATHS_MAX:
        return refusal("BAD_ARGUMENTS", session_id, message=f"paths: 1 - {INFO_PATHS_MAX}")
    infos: list[dict[str, Any]] = []
    for path in paths:
        target = _target(rt, session_id, path, (*rt.config.read_roots, rt.home / RUNS_DIR))
        if isinstance(target, Reply):
            if target.header.get("error") == "BAD_SESSION_ID":
                return target
            infos.append({"path": path, **target.header})
            continue
        if not target.is_file():
            infos.append({"path": str(target), "error": "NOT_FOUND"})
            continue
        stats = file_stats(target)
        infos.append({"path": str(target), "lines": stats.lines, "bytes": stats.size, "lineEnding": stats.line_ending,
                      "bom": stats.bom, "binary": stats.binary, "modified": stamp(target.stat().st_mtime)})
    return Reply({"files": infos}, None, False, None, session_id)
