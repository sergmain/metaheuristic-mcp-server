"""The server's lifetime is its client's: when the client process ends - without closing anything first - the server
exits on its own. The client here is a plain child process (no job object, unlike the SDK's Windows stdio_client), so
nothing but the server itself can end the server."""

import json
import subprocess
import sys
import time
from pathlib import Path

from mh_mcp_server.processes import process_alive
from support import SRC, write_home

SID = "20261023-170000-0c1e17"

# argv: home, out (JSON with the server's pid and creation), src, mode ("idle" | "in-flight")
CLIENT = r'''
import json, os, subprocess, sys, time
from pathlib import Path

home, out, src, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
sys.path.insert(0, src)
from mh_mcp_server.processes import process_created

env = {**os.environ, "MH_MCP_SERVER_HOME": str(home), "PYTHONPATH": src}
err = open(out.with_suffix(".err"), "wb")
server = subprocess.Popen([sys.executable, "-m", "mh_mcp_server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=err, env=env)


def send(message):
    server.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    server.stdin.flush()


send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "client-exit-test", "version": "0"}}})
answer = json.loads(server.stdout.readline())
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
if mode == "in-flight":
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
          "params": {"name": "executeCommand",
                     "arguments": {"sessionId": "''' + SID + r'''", "command": "Start-Sleep 15; 'late'"}}})
    pid_json = home / "runs" / "''' + SID + r'''" / "0001" / "pid.json"
    deadline = time.monotonic() + 15
    while not pid_json.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
out.write_text(json.dumps({"pid": server.pid, "created": process_created(server.pid),
                           "initialized": "result" in answer,
                           "inFlight": mode == "in-flight" and pid_json.is_file()}), encoding="utf-8")
os._exit(0)   # the client ends abruptly: no stdin close, no shutdown request - the OS closes its handles
'''


def run_client(tmp_path: Path, mode: str) -> dict:
    home = write_home(tmp_path)
    script = tmp_path / "client.py"
    script.write_text(CLIENT, encoding="utf-8")
    out = tmp_path / "server.json"
    subprocess.run([sys.executable, str(script), str(home), str(out), str(SRC), mode], timeout=60, check=True)
    return json.loads(out.read_text(encoding="utf-8"))


def seconds_until_gone(pid: int, created: int | None, limit: float) -> float | None:
    """Seconds until `pid` (created at `created`) is no longer running; None when it still runs after `limit`."""
    start = time.monotonic()
    while time.monotonic() - start < limit:
        if not process_alive(pid, created):
            return time.monotonic() - start
        time.sleep(0.1)
    return None


def test_an_idle_server_exits_when_its_client_process_ends(tmp_path: Path):
    server = run_client(tmp_path, "idle")
    assert server["initialized"], server
    gone = seconds_until_gone(server["pid"], server["created"], 20)
    assert gone is not None and gone < 10, f"server pid {server['pid']} still running {gone=}"


def test_a_server_with_a_call_in_flight_exits_when_its_client_process_ends(tmp_path: Path):
    server = run_client(tmp_path, "in-flight")
    assert server["initialized"] and server["inFlight"], server
    gone = seconds_until_gone(server["pid"], server["created"], 45)
    assert gone is not None and gone < 10, f"server pid {server['pid']} still running {gone=}"
