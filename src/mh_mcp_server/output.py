"""Returned output: decoding `output.log` and capping it to the line and byte budget (plan 051, decision 16).

`executeCommand` keeps the head and the tail: the whole output when it fits `maxLines` and `maxBytes`, otherwise the
first 20 % and the last 80 % of the budget with one marker line between them. `waitJob` keeps the tail only. The
byte cap always cuts at a line boundary. Files are streamed, so a large log is never loaded whole.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, NamedTuple, Sequence

HEAD_SHARE_PERCENT = 20


class Line(NamedTuple):
    index: int   # 0-based position in the source
    text: str    # decoded, line ending stripped
    bad: int     # undecodable bytes replaced by U+FFFD


@dataclass(frozen=True)
class Captured:
    lines: list[str]        # the returned lines, the marker line included when the output was cut
    total: int              # lines in the source
    omitted: int            # source lines not returned
    encoding_errors: int    # undecodable bytes in the returned lines


def decode_line(raw: bytes) -> tuple[str, int]:
    """One raw line: its `\\n` / `\\r\\n` stripped, UTF-8 with replacement; and the number of undecodable bytes."""
    if raw.endswith(b"\n"):
        raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
    text = raw.decode("utf-8", errors="surrogateescape")
    bad = sum(1 for ch in text if "\udc80" <= ch <= "\udcff")
    if bad:
        text = "".join("\ufffd" if "\udc80" <= ch <= "\udcff" else ch for ch in text)
    return text, bad


def marker(omitted: int, log_path: Path | str) -> str:
    return f"... {omitted} lines omitted - full output: {log_path} ..."


def cap_head_tail(head_source: Sequence[Line], tail_source: Sequence[Line], total: int, max_lines: int,
                  max_bytes: int, log_path: Path | str) -> Captured:
    """The head and tail within the budget. `head_source` is the source's first lines, `tail_source` its last ones
    (they may overlap, or be the same sequence); `total` is the source's line count."""
    if total <= len(head_source) and total <= max_lines and _size(head_source) <= max_bytes:
        return _captured(list(head_source), total)
    budget_lines = max(max_lines - 1, 0)
    head_lines = budget_lines * HEAD_SHARE_PERCENT // 100
    budget_bytes = max(max_bytes - _line_size(marker(total, log_path)), 0)
    head_bytes = budget_bytes * HEAD_SHARE_PERCENT // 100
    head = _take_head(head_source, head_lines, head_bytes)
    tail = [line for line in _take_tail(tail_source, budget_lines - head_lines, budget_bytes - head_bytes)
            if line.index >= len(head)]
    omitted = total - len(head) - len(tail)
    if omitted <= 0:
        return _captured(head + tail, total)
    lines = [line.text for line in head] + [marker(omitted, log_path)] + [line.text for line in tail]
    return Captured(lines=lines, total=total, omitted=omitted, encoding_errors=sum(l.bad for l in head + tail))


def cap_tail(source: Sequence[Line], max_lines: int, max_bytes: int) -> tuple[list[Line], int]:
    """The last lines of `source` within the budget, and how many of `source` were left out. No marker: the
    caller's line cursor already says where the returned lines start."""
    tail = _take_tail(source, max_lines, max_bytes)
    return tail, len(source) - len(tail)


def read_head_tail(path: Path, max_lines: int, max_bytes: int, log_path: Path | str | None = None) -> Captured:
    """`path` decoded and capped head-and-tail, streamed: at most `max_lines` lines of head and of tail are held."""
    head: list[Line] = []
    tail: deque[Line] = deque(maxlen=max(max_lines, 1))
    total = 0
    for line in iter_lines(path, include_partial=True):
        if total < max_lines:
            head.append(line)
        tail.append(line)
        total += 1
    return cap_head_tail(head, list(tail), total, max_lines, max_bytes, path if log_path is None else log_path)


def read_lines_from(path: Path, since_line: int, include_partial: bool) -> tuple[list[Line], int]:
    """The lines of `path` after its first `since_line`, and the number of lines it has.

    A last line without a line ending is counted only with `include_partial` - while a job runs it may still be
    being written, so it is returned once it ends (or once the job is done)."""
    out: list[Line] = []
    total = 0
    for line in iter_lines(path, include_partial):
        if line.index >= since_line:
            out.append(line)
        total += 1
    return out, total


def iter_lines(path: Path, include_partial: bool) -> Iterator[Line]:
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return
    with f:
        index = 0
        for raw in f:
            if not raw.endswith(b"\n") and not include_partial:
                return
            text, bad = decode_line(raw)
            yield Line(index, text, bad)
            index += 1


def _captured(lines: list[Line], total: int) -> Captured:
    return Captured(lines=[line.text for line in lines], total=total, omitted=total - len(lines),
                    encoding_errors=sum(line.bad for line in lines))


def _line_size(text: str) -> int:
    return len(text.encode("utf-8")) + 1


def _size(lines: Sequence[Line]) -> int:
    return sum(_line_size(line.text) for line in lines)


def _take_head(lines: Sequence[Line], max_lines: int, max_bytes: int) -> list[Line]:
    out: list[Line] = []
    used = 0
    for line in lines[:max_lines]:
        size = _line_size(line.text)
        if used + size > max_bytes:
            break
        out.append(line)
        used += size
    return out


def _take_tail(lines: Sequence[Line], max_lines: int, max_bytes: int) -> list[Line]:
    out: list[Line] = []
    used = 0
    for line in reversed(lines[len(lines) - max_lines:] if max_lines > 0 else []):
        size = _line_size(line.text)
        if used + size > max_bytes:
            break
        out.append(line)
        used += size
    out.reverse()
    return out
