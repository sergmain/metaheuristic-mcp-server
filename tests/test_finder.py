"""searchFiles, listFiles, fileInfo (0.1.10) over synthetic trees the tests build in tmp_path (inside the read roots)."""

from pathlib import Path

import pytest

from mh_mcp_server.config import load_config, read_text_if_exists
from mh_mcp_server.execution import production_runtime
from mh_mcp_server.finder import file_info, globs_of, list_files, search_files
from support import write_home

SID = "20261026-170000-00010a"


def runtime(tmp_path: Path):
    home = write_home(tmp_path)
    return production_runtime(home, load_config(home, read_text_if_exists))


def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8", newline="\n")
    (root / "a" / "b" / "y.java").write_text("class Y {}\nint f;\n", encoding="utf-8", newline="\n")
    (root / "target").mkdir()
    (root / "target" / "z.py").write_text("def f(): pass\n", encoding="utf-8", newline="\n")
    (root / ".git").mkdir()
    (root / ".git" / "k").write_text("f\n", encoding="utf-8", newline="\n")
    (root / "bin.dat").write_bytes(b"f \x00 f")
    return root


# ==================== searchFiles ====================

def test_search_finds_every_match_by_relative_path_and_skips_build_dirs_and_binaries(tmp_path: Path):
    reply = search_files(runtime(tmp_path), SID, str(tree(tmp_path)), r"\bf\b")

    assert reply.text.splitlines() == ["a/x.py:1: def f():", "a/b/y.java:2: int f;"]
    assert (reply.header["matches"], reply.header["files"], reply.header["scanned"], reply.header["skippedBinary"]) == (2, 2, 2, 1)
    assert "truncated" not in reply.header


def test_search_glob_case_context_and_included_skipped_dirs(tmp_path: Path):
    rt, root = runtime(tmp_path), tree(tmp_path)

    assert search_files(rt, SID, str(root), r"\bf\b", glob="*.java").text == "a/b/y.java:2: int f;"
    assert search_files(rt, SID, str(root), "CLASS", ignore_case=True).text == "a/b/y.java:1: class Y {}"
    assert search_files(rt, SID, str(root), "def f", glob="x.py", context=1).text.splitlines() == \
        ["a/x.py:1: def f():", "a/x.py:2-     return 1"]
    assert "target/z.py:1: def f(): pass" in search_files(rt, SID, str(root), "def f", include_skipped=True).text.splitlines()


def test_search_stops_at_max_results_and_says_so(tmp_path: Path):
    reply = search_files(runtime(tmp_path), SID, str(tree(tmp_path)), r"\bf\b", max_results=1)

    assert reply.text == "a/x.py:1: def f():"
    assert reply.header["truncated"] is True


def test_search_one_file_as_its_root(tmp_path: Path):
    root = tree(tmp_path)
    assert search_files(runtime(tmp_path), SID, str(root / "a" / "x.py"), "return").text == "x.py:2:     return 1"


@pytest.mark.parametrize("args, kind", [
    ({"regex": "("}, "BAD_ARGUMENTS"),
    ({"regex": "f", "max_results": 0}, "BAD_ARGUMENTS"),
    ({"regex": "f", "context": 11}, "BAD_ARGUMENTS"),
])
def test_search_refuses_bad_arguments(tmp_path: Path, args, kind):
    reply = search_files(runtime(tmp_path), SID, str(tree(tmp_path)), **args)
    assert reply.is_error and reply.header["error"] == kind


def test_search_refuses_a_root_outside_the_roots_or_missing(tmp_path: Path):
    rt = runtime(tmp_path)
    outside = search_files(rt, SID, str(Path(tmp_path.anchor) / "no-such-root-mh-mcp"), "f")
    assert outside.is_error and outside.header["error"] == "PATH_OUTSIDE_ROOTS"
    missing = search_files(rt, SID, str(tmp_path / "missing"), "f")
    assert missing.is_error and missing.header["error"] == "NOT_FOUND"
    assert search_files(rt, "bad", str(tmp_path), "f").header == {"error": "BAD_SESSION_ID"}


# ==================== listFiles ====================

def names(text: str) -> list[str]:
    return [line.split("  ")[-1] for line in text.splitlines()]


def test_list_one_level_names_dirs_with_a_slash_and_lists_skipped_dirs_without_entering(tmp_path: Path):
    reply = list_files(runtime(tmp_path), SID, str(tree(tmp_path)))

    assert names(reply.text) == [".git/", "a/", "bin.dat", "target/"]
    assert (reply.header["dirs"], reply.header["files"]) == (3, 1)
    assert reply.text.splitlines()[2].split()[:2] == ["f", "5"], "type and size of bin.dat"


def test_list_recursive_glob_is_a_find(tmp_path: Path):
    rt, root = runtime(tmp_path), tree(tmp_path)
    assert names(list_files(rt, SID, str(root), glob="*.py, *.java", max_depth=5).text) == ["a/x.py", "a/b/y.java"]
    assert names(list_files(rt, SID, str(root), glob="*.py", max_depth=5, include_skipped=True).text) == \
        ["a/x.py", "target/z.py"]
    capped = list_files(rt, SID, str(root), max_results=2)
    assert names(capped.text) == [".git/", "a/"] and capped.header["truncated"] is True


def test_globs_of():
    assert globs_of(None) == [] and globs_of(" ") == []
    assert globs_of("*.java, *.py,") == ["*.java", "*.py"]


# ==================== fileInfo ====================

def test_file_info_counts_several_files_and_reports_each_failure_in_its_own_entry(tmp_path: Path):
    rt, root = runtime(tmp_path), tree(tmp_path)
    (root / "crlf.txt").write_bytes(b"one\r\ntwo")

    infos = file_info(rt, SID, [str(root / "a" / "x.py"), str(root / "crlf.txt"), str(root / "none.txt"),
                                str(Path(tmp_path.anchor) / "no-such-root-mh-mcp" / "f.txt")]).header["files"]

    assert {k: infos[0][k] for k in ("lines", "bytes", "lineEnding", "bom", "binary")} == \
        {"lines": 2, "bytes": 22, "lineEnding": "lf", "bom": False, "binary": False}
    assert (infos[1]["lines"], infos[1]["bytes"], infos[1]["lineEnding"]) == (2, 8, "crlf")
    assert infos[2]["error"] == "NOT_FOUND"
    assert infos[3]["error"] == "PATH_OUTSIDE_ROOTS"


def test_file_info_refuses_no_paths_or_too_many(tmp_path: Path):
    rt = runtime(tmp_path)
    assert file_info(rt, SID, []).header["error"] == "BAD_ARGUMENTS"
    assert file_info(rt, SID, ["x"] * 51).header["error"] == "BAD_ARGUMENTS"
