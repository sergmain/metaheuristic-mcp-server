import hashlib
import shutil
import time
from pathlib import Path

import pytest

from mh_mcp_server.config import load_config, read_text_if_exists
from mh_mcp_server.execution import production_runtime
from mh_mcp_server.files import edit_file, file_stats, offset_of_line, read_file, write_file
from support import REPO, write_home

FIXTURES = REPO / "tests" / "fixtures"
SID = "20261023-180000-0000ff"


def runtime(tmp_path: Path, extra_toml: str = ""):
    home = write_home(tmp_path, extra_toml)
    return production_runtime(home, load_config(home, read_text_if_exists))


def copy(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copyfile(FIXTURES / name, target)
    return target


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ==================== readFile ====================

@pytest.mark.parametrize("name, lines, ending, bom", [
    ("crlf.txt", 3, "crlf", False),
    ("lf.txt", 3, "lf", False),
    ("mixed.txt", 4, "mixed", False),
    ("bom-crlf.txt", 2, "crlf", True),
    ("lines-434.txt", 434, "lf", False),
])
def test_read_file_reports_the_real_line_count_ending_and_bom(tmp_path: Path, name, lines, ending, bom):
    header = read_file(runtime(tmp_path), SID, str(FIXTURES / name)).header
    assert (header["totalLines"], header["lineEnding"], header["bom"]) == (lines, ending, bom)


def test_read_file_numbers_the_lines_and_strips_endings_and_the_bom(tmp_path: Path):
    reply = read_file(runtime(tmp_path), SID, str(FIXTURES / "bom-crlf.txt"))
    assert reply.text == "1: Привет\n2: world"
    assert reply.header["fromLine"] == 1 and reply.header["toLine"] == 2 and "nextFromLine" not in reply.header


def test_read_file_stops_at_the_line_budget_and_returns_next_from_line(tmp_path: Path):
    rt = runtime(tmp_path)
    first = read_file(rt, SID, str(FIXTURES / "lines-434.txt"))
    assert (first.header["toLine"], first.header["nextFromLine"]) == (400, 401)
    second = read_file(rt, SID, str(FIXTURES / "lines-434.txt"), from_line=401)
    assert second.header["fromLine"] == 401 and second.header["toLine"] == 434 and "nextFromLine" not in second.header
    assert second.text.splitlines()[0] == "401: line 401" and second.text.splitlines()[-1] == "434: line 434"


def test_read_file_stops_at_the_byte_budget_on_a_line_boundary(tmp_path: Path):
    rt = runtime(tmp_path, "[read]\nmaxBytes = 100\n")
    reply = read_file(rt, SID, str(FIXTURES / "lines-434.txt"))
    # "N: line 00N" is 13-15 bytes + newline
    assert all(entry.endswith(f"line {int(entry.split(':')[0]):03d}") for entry in reply.text.splitlines())
    assert sum(len(e) + 1 for e in reply.text.splitlines()) <= 100
    assert reply.header["nextFromLine"] == reply.header["toLine"] + 1


def test_read_file_from_a_negative_line_returns_the_last_lines(tmp_path: Path):
    reply = read_file(runtime(tmp_path), SID, str(FIXTURES / "lines-434.txt"), from_line=-3)
    assert reply.text == "432: line 432\n433: line 433\n434: line 434"
    assert reply.header["fromLine"] == 432


def test_read_file_a_range(tmp_path: Path):
    reply = read_file(runtime(tmp_path), SID, str(FIXTURES / "lines-434.txt"), from_line=10, to_line=12)
    assert reply.text == "10: line 010\n11: line 011\n12: line 012" and "nextFromLine" not in reply.header


def test_read_file_from_line_minus_50_of_a_200_mb_log_is_fast(tmp_path: Path):
    log = tmp_path / "big.log"
    line = b"x" * 99 + b"\n"
    with open(log, "wb") as f:
        block = line * 10_000
        for _ in range(210):
            f.write(block)
    assert log.stat().st_size >= 200 * 1024 * 1024
    started = time.monotonic()
    reply = read_file(runtime(tmp_path), SID, str(log), from_line=-50)
    assert time.monotonic() - started < 10
    assert reply.header["totalLines"] == 2_100_000
    assert reply.header["fromLine"] == 2_100_000 - 49 and reply.header["toLine"] == 2_100_000
    assert len(reply.text.splitlines()) == 50


def test_read_file_refusals(tmp_path: Path):
    rt = runtime(tmp_path)
    assert read_file(rt, SID, "C:/Windows/win.ini").header["error"] == "PATH_OUTSIDE_ROOTS"
    assert read_file(rt, SID, "C:/sandbox/_work2/x.txt").header["error"] == "PATH_OUTSIDE_ROOTS"
    assert read_file(rt, SID, str(tmp_path / "absent.txt")).header == {"error": "NOT_FOUND",
                                                                        "path": str(tmp_path / "absent.txt")}
    (tmp_path / "bin.dat").write_bytes(b"ab\x00cd")
    assert read_file(rt, SID, str(tmp_path / "bin.dat")).header["error"] == "BINARY_FILE"


def test_a_relative_path_is_relative_to_the_session_cwd(tmp_path: Path):
    (tmp_path / "rel.txt").write_bytes(b"r\n")
    assert read_file(runtime(tmp_path), SID, "rel.txt").text == "1: r"


def test_file_stats_counts_crlf_split_across_chunks(tmp_path: Path):
    path = tmp_path / "split.txt"
    path.write_bytes(b"a" * (8192 - 1) + b"\r\n" + b"b\r\n")    # the first CR ends the 8 KB probe chunk
    stats = file_stats(path)
    assert (stats.crlf, stats.lf, stats.lines) == (2, 0, 2)


def test_offset_of_line(tmp_path: Path):
    path = tmp_path / "o.txt"
    path.write_bytes(b"ab\ncd\nef")
    assert [offset_of_line(path, n) for n in (1, 2, 3, 4)] == [0, 3, 6, 8]


# ==================== editFile ====================

def test_an_edit_on_a_crlf_file_changes_no_byte_outside_the_replaced_text(tmp_path: Path):
    path = copy(tmp_path, "crlf.txt")
    before = path.read_bytes()
    reply = edit_file(runtime(tmp_path), SID, str(path), [{"old": "two", "new": "2"}])
    assert reply.header["edits"] == [{"index": 0, "matches": 1, "line": 2}]
    assert path.read_bytes() == before.replace(b"two", b"2")


def test_a_multi_line_edit_on_a_crlf_file_written_with_lf_matches_and_keeps_crlf(tmp_path: Path):
    path = copy(tmp_path, "crlf.txt")
    edit_file(runtime(tmp_path), SID, str(path), [{"old": "line one\nline two", "new": "first\nsecond\nthird"}])
    assert path.read_bytes() == b"first\r\nsecond\r\nthird\r\nline three\r\n"


def test_a_new_holding_crlf_on_a_crlf_file_leaves_no_double_cr(tmp_path: Path):
    path = copy(tmp_path, "crlf.txt")
    edit_file(runtime(tmp_path), SID, str(path), [{"old": "line two\r\n", "new": "x\r\ny\r\n"}])
    data = path.read_bytes()
    assert b"\r\r\n" not in data
    assert data == b"line one\r\nx\r\ny\r\nline three\r\n"


def test_a_mixed_file_is_refused_with_both_counts(tmp_path: Path):
    path = copy(tmp_path, "mixed.txt")
    reply = edit_file(runtime(tmp_path), SID, str(path), [{"old": "a", "new": "b"}])
    assert reply.is_error and reply.header == {"error": "MIXED_LINE_ENDINGS", "crlf": 2, "lf": 2}


def test_match_count_leaves_the_file_unchanged(tmp_path: Path):
    path = copy(tmp_path, "lf.txt")
    digest = sha(path)
    reply = edit_file(runtime(tmp_path), SID, str(path), [{"old": "line", "new": "L"}])
    assert reply.header == {"error": "MATCH_COUNT", "index": 0, "expected": 1, "found": 3, "old": "line"}
    assert sha(path) == digest


def test_edits_apply_in_order_and_all_or_nothing(tmp_path: Path):
    path = copy(tmp_path, "lf.txt")
    rt = runtime(tmp_path)
    digest = sha(path)
    reply = edit_file(rt, SID, str(path), [{"old": "one", "new": "1"}, {"old": "one", "new": "x"}])
    assert reply.header["error"] == "MATCH_COUNT" and reply.header["index"] == 1 and sha(path) == digest
    reply = edit_file(rt, SID, str(path), [{"old": "one", "new": "two-a"}, {"old": "two", "new": "2", "count": 2}])
    assert path.read_bytes() == b"line 2-a\nline 2\nline three\n"
    assert reply.header["edits"] == [{"index": 0, "matches": 1, "line": 1}, {"index": 1, "matches": 2, "line": 1}]


def test_a_lone_cr_in_old_or_new_is_refused(tmp_path: Path):
    path = copy(tmp_path, "lf.txt")
    assert edit_file(runtime(tmp_path), SID, str(path), [{"old": "one", "new": "a\rb"}]).header == \
        {"error": "LONE_CR", "index": 0}


def test_a_bom_survives_an_edit_and_an_overwrite(tmp_path: Path):
    path = copy(tmp_path, "bom-crlf.txt")
    rt = runtime(tmp_path)
    edit_file(rt, SID, str(path), [{"old": "world", "new": "мир"}])
    assert path.read_bytes() == b"\xef\xbb\xbf" + "Привет\r\nмир\r\n".encode("utf-8")
    write_file(rt, SID, str(path), "new\ncontent\n", mode="overwrite")
    assert path.read_bytes() == b"\xef\xbb\xbfnew\r\ncontent\r\n"


def test_dry_run_writes_nothing(tmp_path: Path):
    path = copy(tmp_path, "lf.txt")
    digest = sha(path)
    reply = edit_file(runtime(tmp_path), SID, str(path), [{"old": "two", "new": "2"}], dry_run=True)
    assert reply.header["dryRun"] is True and reply.header["edits"][0]["line"] == 2 and sha(path) == digest


def test_edit_keeps_bytes_that_are_not_utf8(tmp_path: Path):
    path = tmp_path / "latin1.txt"
    path.write_bytes(b"caf\xe9\nline two\n")
    edit_file(runtime(tmp_path), SID, str(path), [{"old": "two", "new": "2"}])
    assert path.read_bytes() == b"caf\xe9\nline 2\n"


# ==================== writeFile ====================

def test_create_on_an_existing_file_is_refused(tmp_path: Path):
    path = copy(tmp_path, "lf.txt")
    digest = sha(path)
    assert write_file(runtime(tmp_path), SID, str(path), "x").header == {"error": "EXISTS", "path": str(path)}
    assert sha(path) == digest


def test_write_creates_parents_with_lf_and_normalizes_crlf_content(tmp_path: Path):
    path = tmp_path / "a" / "b" / "new.txt"
    reply = write_file(runtime(tmp_path), SID, str(path), "one\r\ntwo\nПривет\n")
    assert path.read_bytes() == "one\ntwo\nПривет\n".encode("utf-8")
    assert reply.header == {"path": str(path), "bytes": len(path.read_bytes()), "lines": 3, "lineEnding": "lf"}


def test_overwrite_keeps_a_crlf_files_ending_unless_told_otherwise(tmp_path: Path):
    rt = runtime(tmp_path)
    path = copy(tmp_path, "crlf.txt")
    write_file(rt, SID, str(path), "a\nb\n", mode="overwrite")
    assert path.read_bytes() == b"a\r\nb\r\n"
    write_file(rt, SID, str(path), "a\r\nb\r\n", mode="overwrite", line_ending="lf")
    assert path.read_bytes() == b"a\nb\n"
    write_file(rt, SID, str(tmp_path / "c.txt"), "a\nb", line_ending="crlf")
    assert (tmp_path / "c.txt").read_bytes() == b"a\r\nb"


def test_write_refusals(tmp_path: Path):
    rt = runtime(tmp_path)
    assert write_file(rt, SID, "C:/Windows/x.txt", "x").header["error"] == "PATH_OUTSIDE_ROOTS"
    assert write_file(rt, SID, str(tmp_path / "x.txt"), "x", mode="append").header["error"] == "BAD_ARGUMENTS"
    assert write_file(rt, SID, str(tmp_path), "x", mode="overwrite").header["error"] == "WRITE_FAILED"


# ==================== [excluded] paths ====================

def test_the_file_tools_refuse_an_excluded_path_inside_the_roots(tmp_path: Path):
    secret = tmp_path / "Secret"
    secret.mkdir()
    (secret / "a.txt").write_bytes(b"keep\n")
    item = str(secret).replace("\\", "/").lower()   # another case and other separators than the target's
    rt = runtime(tmp_path, f'[excluded]\npaths = ["{item}"]\n')
    expected = {"error": "PATH_EXCLUDED", "path": str(secret / "a.txt"), "excluded": item}
    assert read_file(rt, SID, str(secret / "a.txt")).header == expected
    assert edit_file(rt, SID, str(secret / "a.txt"), [{"old": "keep", "new": "x"}]).header == expected
    assert write_file(rt, SID, str(secret / "a.txt"), "x", mode="overwrite").header == expected
    assert write_file(rt, SID, "Secret/new.txt", "x").header["error"] == "PATH_EXCLUDED"   # relative path too
    assert (secret / "a.txt").read_bytes() == b"keep\n" and not (secret / "new.txt").exists()
    (tmp_path / "open.txt").write_bytes(b"o\n")
    assert read_file(rt, SID, str(tmp_path / "open.txt")).text == "1: o"
