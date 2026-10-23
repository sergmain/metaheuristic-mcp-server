from pathlib import Path

import pytest

from mh_mcp_server.output import (Line, cap_head_tail, cap_tail, decode_line, marker, read_head_tail,
                                  read_lines_from)

LOG = "C:/_mh/mh-mcp-server/runs/s/0001/output.log"


def lines_of(texts) -> list[Line]:
    return [Line(i, t, 0) for i, t in enumerate(texts)]


def test_the_whole_output_is_returned_when_it_fits():
    source = lines_of(["a", "b", "c"])
    c = cap_head_tail(source, source, 3, 150, 24576, LOG)
    assert (c.lines, c.total, c.omitted) == (["a", "b", "c"], 3, 0)


def test_the_cut_keeps_the_first_20_and_the_last_80_percent_of_the_line_budget():
    source = lines_of([f"line {i}" for i in range(1000)])
    c = cap_head_tail(source, source, 1000, 150, 10 ** 9, LOG)
    # 149 lines of budget beside the marker: 29 head, 120 tail
    assert c.lines[:29] == [f"line {i}" for i in range(29)]
    assert c.lines[29] == marker(851, LOG)
    assert c.lines[30:] == [f"line {i}" for i in range(880, 1000)]
    assert (c.total, c.omitted) == (1000, 851)
    assert len(c.lines) == 150


def test_the_marker_line_names_the_count_and_the_log():
    assert marker(851, LOG) == f"... 851 lines omitted - full output: {LOG} ..."


def test_the_byte_cap_cuts_at_a_line_boundary():
    source = lines_of([f"{i:03d}" + "x" * 96 for i in range(50)])   # 99 chars + newline = 100 bytes each
    c = cap_head_tail(source, source, 50, 150, 2000, LOG)
    kept = [line for line in c.lines if not line.startswith("...")]
    assert all(len(line) == 99 and line in [s.text for s in source] for line in kept)
    assert sum(len(line.encode("utf-8")) + 1 for line in c.lines) <= 2000
    assert c.omitted == 50 - len(kept)
    assert c.lines[0].startswith("000") and c.lines[-1].startswith("049")


def test_the_byte_cap_counts_utf8_bytes_not_characters():
    source = lines_of(["Ж" * 100] * 10)     # 201 bytes per line
    c = cap_head_tail(source, source, 10, 150, 1000, LOG)
    assert sum(len(line.encode("utf-8")) + 1 for line in c.lines) <= 1000
    assert c.omitted > 0


def test_head_and_tail_never_repeat_a_line():
    source = lines_of([f"l{i}" for i in range(10)])
    c = cap_head_tail(source, source, 10, 4, 10 ** 9, LOG)
    texts = [line for line in c.lines if not line.startswith("...")]
    assert len(texts) == len(set(texts))
    assert c.omitted == 10 - len(texts)


def test_cap_tail_keeps_the_last_lines_within_lines_and_bytes():
    source = lines_of([f"l{i}" for i in range(10)])
    kept, left_out = cap_tail(source, 3, 10 ** 9)
    assert [line.text for line in kept] == ["l7", "l8", "l9"] and left_out == 7
    kept, left_out = cap_tail(source, 10, 7)
    assert [line.text for line in kept] == ["l8", "l9"] and left_out == 8


@pytest.mark.parametrize("raw, text, bad", [
    (b"abc\r\n", "abc", 0),
    (b"abc\n", "abc", 0),
    (b"abc", "abc", 0),
    ("Привет\r\n".encode("utf-8"), "Привет", 0),
    (b"a\xffb\xfe\n", "a\ufffdb\ufffd", 2),
    (b"a\r\r\n", "a\r", 0),
])
def test_decode_line(raw: bytes, text: str, bad: int):
    assert decode_line(raw) == (text, bad)


def test_read_head_tail_streams_a_real_file(tmp_path: Path):
    path = tmp_path / "output.log"
    with open(path, "wb") as f:
        for i in range(100_000):
            f.write(f"line {i}\r\n".encode("ascii"))
    c = read_head_tail(path, 150, 24576)
    assert c.total == 100_000
    assert c.lines[0] == "line 0" and c.lines[-1] == "line 99999"
    assert c.lines[29] == marker(100_000 - 149, path)


def test_read_head_tail_reports_encoding_errors_of_the_returned_lines(tmp_path: Path):
    path = tmp_path / "output.log"
    path.write_bytes(b"ok\r\nbad \xff\r\n")
    c = read_head_tail(path, 150, 24576)
    assert c.lines == ["ok", "bad \ufffd"] and c.encoding_errors == 1


def test_read_head_tail_of_a_missing_file_is_empty(tmp_path: Path):
    c = read_head_tail(tmp_path / "absent.log", 150, 24576)
    assert (c.lines, c.total, c.omitted) == ([], 0, 0)


def test_read_lines_from_skips_the_cursor_and_a_partial_last_line(tmp_path: Path):
    path = tmp_path / "output.log"
    path.write_bytes(b"a\r\nb\r\nc\r\npart")
    lines, total = read_lines_from(path, 1, include_partial=False)
    assert [line.text for line in lines] == ["b", "c"] and total == 3
    lines, total = read_lines_from(path, 3, include_partial=True)
    assert [line.text for line in lines] == ["part"] and total == 4
