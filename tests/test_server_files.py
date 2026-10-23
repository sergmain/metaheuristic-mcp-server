import shutil
from pathlib import Path

import anyio

from support import REPO, call, connect, write_home

FIXTURES = REPO / "tests" / "fixtures"
SID = "20261023-181500-0000ee"
EIGHT = ["editFile", "executeCommand", "getSession", "getSessionId", "readFile", "startJob", "waitJob", "writeFile"]


def test_tools_list_returns_all_eight_tools(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            assert sorted(t.name for t in (await session.list_tools()).tools) == EIGHT

    anyio.run(main)


def test_read_file_of_the_434_line_fixture_in_two_calls(tmp_path: Path):
    async def main():
        async with connect(write_home(tmp_path)) as session:
            path = str(FIXTURES / "lines-434.txt")
            header, text, is_error = await call(session, "readFile", sessionId=SID, path=path)
            assert not is_error
            assert (header["totalLines"], header["toLine"], header["nextFromLine"]) == (434, 400, 401)
            assert len(text.splitlines()) == 400
            header, text, _ = await call(session, "readFile", sessionId=SID, path=path, fromLine=401)
            assert (header["fromLine"], header["toLine"]) == (401, 434) and "nextFromLine" not in header
            assert text.splitlines() == [f"{n}: line {n:03d}" for n in range(401, 435)]

    anyio.run(main)


def test_write_and_edit_round_trip_through_stdio(tmp_path: Path):
    target = tmp_path / "doc.txt"
    shutil.copyfile(FIXTURES / "crlf.txt", target)

    async def main():
        async with connect(write_home(tmp_path)) as session:
            header, _, is_error = await call(session, "editFile", sessionId=SID, path="doc.txt",
                                             edits=[{"old": "line two", "new": "строка два"}])
            assert not is_error, header
            assert header["edits"] == [{"index": 0, "matches": 1, "line": 2}]
            header, _, is_error = await call(session, "writeFile", sessionId=SID, path="out/new.txt",
                                             content="a\nб\n")
            assert not is_error and header["lineEnding"] == "lf" and header["lines"] == 2
            header, _, is_error = await call(session, "editFile", sessionId=SID, path="doc.txt",
                                             edits=[{"old": "absent", "new": "x"}])
            assert is_error and header["error"] == "MATCH_COUNT"

    anyio.run(main)
    assert target.read_bytes() == "line one\r\nстрока два\r\nline three\r\n".encode("utf-8")
    assert (tmp_path / "out" / "new.txt").read_bytes() == "a\nб\n".encode("utf-8")
