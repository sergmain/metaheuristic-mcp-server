import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio

from mh_mcp_server.transcript import append, format_block, transcript_path

REPO = Path(__file__).resolve().parent.parent
SID = "20261023-154207-0a1b2c"
AT = datetime(2026, 10, 23, 15, 42, 7, 512000, tzinfo=timezone(timedelta(hours=3)))


def test_format_block_with_a_call_id_and_raw_text():
    block = format_block(AT, "0007", "executeCommand", {"sessionId": SID, "command": "'A'"},
                         {"callId": "0007", "exitCode": 0}, 1.844, text="A\nB")
    assert block == (
        "=== 2026-10-23T15:42:07.512+03:00 | call 0007 | executeCommand | 1.84 s\n"
        "--- request\n"
        '{\n  "sessionId": "20261023-154207-0a1b2c",\n  "command": "\'A\'"\n}\n'
        "--- response\n"
        '{\n  "callId": "0007",\n  "exitCode": 0\n}\n'
        "A\nB\n"
    )


def test_format_block_without_a_call_id_and_without_text():
    block = format_block(AT, None, "getSessionId", {}, {"sessionId": SID}, 0.004)
    assert block.splitlines()[0] == "=== 2026-10-23T15:42:07.512+03:00 | getSessionId | 0.00 s"
    assert block.endswith('"sessionId": "20261023-154207-0a1b2c"\n}\n')


def test_format_block_writes_a_str_response_as_it_is_and_uses_lf_only():
    block = format_block(AT, "0001", "executeCommand", {}, "tool failed:\r\nboom", 0.1, text="x\r\ny\r\n")
    assert "\r" not in block
    assert block.endswith("--- response\ntool failed:\nboom\nx\ny\n")


def test_a_cyrillic_request_round_trips_byte_exact(tmp_path: Path):
    request = {"sessionId": SID, "command": "$env:MCP_T = 'бар'; \"Привет\""}
    block = format_block(AT, "0002", "executeCommand", request, {"exitCode": 0}, 0.5, text="Привет")
    anyio.run(append, tmp_path, SID, block)
    data = transcript_path(tmp_path, SID).read_bytes()
    assert data == block.encode("utf-8")
    assert not data.startswith(b"\xef\xbb\xbf")
    assert "Привет" in data.decode("utf-8") and "\\u" not in data.decode("utf-8")
    body = data.decode("utf-8").split("--- request\n", 1)[1].split("\n--- response\n", 1)[0]
    assert json.loads(body) == request


def test_appends_add_blocks_in_order(tmp_path: Path):
    async def main():
        for i in range(3):
            await append(tmp_path, SID, format_block(AT, f"{i + 1:04d}", "t", {"i": i}, {}, 0))

    anyio.run(main)
    text = transcript_path(tmp_path, SID).read_text(encoding="utf-8")
    assert [int(m) for m in re.findall(r'"i": (\d+)', text)] == [0, 1, 2]


def parse_blocks(text: str) -> list[dict]:
    blocks = re.split(r"(?m)^=== ", text)
    assert blocks[0] == ""
    parsed = []
    for block in blocks[1:]:
        head, rest = block.split("\n--- request\n", 1)
        request, response = rest.split("\n--- response\n", 1)
        parsed.append({"head": head, "request": json.loads(request), "response": response})
    return parsed


def test_concurrent_appends_in_one_process_never_interleave(tmp_path: Path):
    async def main():
        async with anyio.create_task_group() as tg:
            for i in range(100):
                tg.start_soon(append, tmp_path, SID,
                              format_block(AT, None, "t", {"i": i, "payload": "Ж" * 3000}, "ok", 0))

    anyio.run(main)
    blocks = parse_blocks(transcript_path(tmp_path, SID).read_text(encoding="utf-8"))
    assert sorted(b["request"]["i"] for b in blocks) == list(range(100))
    assert all(b["request"]["payload"] == "Ж" * 3000 and b["response"] == "ok\n" for b in blocks)


WRITER = r"""
import sys, anyio
from datetime import datetime
from pathlib import Path
from mh_mcp_server.transcript import append, format_block

home, sid, writer = Path(sys.argv[1]), sys.argv[2], sys.argv[3]

async def main():
    for i in range(200):
        await append(home, sid, format_block(datetime.now().astimezone(), None, 'w',
                                             {'writer': writer, 'i': i, 'payload': writer * 5000}, 'ok', 0))

anyio.run(main)
"""


def test_two_processes_appending_200_blocks_each_leave_400_whole_blocks(tmp_path: Path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    writers = [subprocess.Popen([sys.executable, "-c", WRITER, str(tmp_path), SID, name], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
               for name in ("A", "B")]
    for w in writers:
        _, err = w.communicate(timeout=120)
        assert w.returncode == 0, err.decode("utf-8", "replace")
    blocks = parse_blocks(transcript_path(tmp_path, SID).read_text(encoding="utf-8"))
    assert len(blocks) == 400
    for writer in ("A", "B"):
        mine = [b for b in blocks if b["request"]["writer"] == writer]
        assert [b["request"]["i"] for b in mine] == list(range(200))
        assert all(b["request"]["payload"] == writer * 5000 and b["response"] == "ok\n" for b in mine)
