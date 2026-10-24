"""A client's notifications/cancelled for an in-flight tools/call must not stop the server from answering later
requests (modelcontextprotocol/python-sdk#2610: RequestResponder.__exit__ re-raises the cancellation and the task
group around the receive loop dies - the process stays alive but never reads stdin again).

The server runs under the SDK's own stdio_client (as in `support.connect`), but messages are written raw, so the test
owns the request ids it cancels."""

from pathlib import Path

import anyio

from support import REPO, SRC, write_home

ATTEMPTS = 12


async def drive_cancellations(home: Path) -> list[str]:
    """One line per attempt: 'answered' or 'NO ANSWER' for the request sent right after a cancelled tools/call."""
    import sys

    from mcp import StdioServerParameters, types
    from mcp.client.stdio import stdio_client
    from mcp.shared.message import SessionMessage

    params = StdioServerParameters(command=sys.executable, args=["-m", "mh_mcp_server"],
                                   env={"MH_MCP_SERVER_HOME": str(home), "PYTHONPATH": str(SRC)}, cwd=str(REPO))
    outcomes: list[str] = []
    async with stdio_client(params) as (read, write):
        async def send(message: dict) -> None:
            await write.send(SessionMessage(types.JSONRPCMessage.model_validate({"jsonrpc": "2.0", **message})))

        async def answer_to(request_id: int, seconds: float) -> dict | None:
            with anyio.move_on_after(seconds):
                while True:
                    try:
                        item = await read.receive()
                    except (anyio.EndOfStream, anyio.ClosedResourceError):
                        return None   # the server process is gone: no answer either
                    if isinstance(item, Exception):
                        continue
                    message = item.message.model_dump(by_alias=True, exclude_none=True)
                    if message.get("id") == request_id:
                        return message
            return None

        await send({"id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "cancel-test", "version": "0"}}})
        assert await answer_to(1, 20) is not None, "no answer to initialize"
        await send({"method": "notifications/initialized"})

        for attempt in range(ATTEMPTS):
            call_id = 100 + 2 * attempt
            await send({"id": call_id, "method": "tools/call",
                        "params": {"name": "executeCommand",
                                   "arguments": {"sessionId": f"20261024-1200{attempt:02d}-c0ffee",
                                                 "command": "Start-Sleep 2"}}})
            await anyio.sleep(0.005 * (attempt % 6))   # vary where the handler is when the cancellation lands
            await send({"method": "notifications/cancelled", "params": {"requestId": call_id, "reason": "test"}})
            try:
                await send({"id": call_id + 1, "method": "tools/list"})
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                outcomes.append("NO ANSWER")   # the server process is gone before the request could be sent
                break
            answered = await answer_to(call_id + 1, 15) is not None
            outcomes.append("answered" if answered else "NO ANSWER")
            if not answered:
                break
    return outcomes


def test_a_cancelled_call_does_not_stop_the_server_answering_later_requests(tmp_path: Path):
    outcomes = anyio.run(drive_cancellations, write_home(tmp_path))
    assert outcomes == ["answered"] * ATTEMPTS, outcomes


def test_the_instance_log_records_every_call_arrival_and_every_ignored_cancellation(tmp_path: Path):
    home = write_home(tmp_path)
    outcomes = anyio.run(drive_cancellations, home)
    assert outcomes == ["answered"] * ATTEMPTS, outcomes
    logs = list((home / "logs").glob("server-*.log"))
    assert len(logs) == 1, logs
    text = logs[0].read_text(encoding="utf-8")
    for attempt in range(ATTEMPTS):
        call_id = 100 + 2 * attempt
        assert f"request {call_id}: executeCommand sessionId=20261024-1200{attempt:02d}-c0ffee arrived" in text, text
        assert f"request {call_id}: notifications/cancelled ignored" in text, text
