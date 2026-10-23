"""The MCP surface: FastMCP("mh-mcp"), the tools, and transcript and masking around every call (plan 051, Phase 7).

A tool result is two text contents (decision 17): content 0 the compact JSON header, content 1 the raw output or file
text, present only when non-empty; a refusal sets isError and carries {"error": "<KIND>", ...} in content 0.

FastMCP registers the tools (input schemas from the signatures, argument validation). Its own CallToolRequest handler
is replaced: in mcp 1.12 that handler can set isError only from an exception, and FastMCP prefixes the message with
"Error executing tool <name>: ", so content 0 could not be the bare JSON header of a refusal.

No `from __future__ import annotations` here: FastMCP 1.12 inspects the tool functions' annotations as classes
(`issubclass(param.annotation, Context)`), which fails on string annotations.
"""

import json
import logging
import time
from datetime import datetime
from typing import Any

import anyio.to_thread
import pydantic
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mh_mcp_server import execution, transcript
from mh_mcp_server.environment import mask
from mh_mcp_server.execution import Reply, Runtime, refusal
from mh_mcp_server.session_id import is_valid_session_id

log = logging.getLogger("mh_mcp_server")

TOOLS_PHASE_7 = ("getSessionId", "getSession", "executeCommand")


def build_server(rt: Runtime) -> FastMCP:
    mcp = FastMCP("mh-mcp", log_level="WARNING")

    @mcp.tool(name="getSessionId", structured_output=False)
    async def get_session_id() -> Reply:
        """Mints a new sessionId (yyyyMMdd-HHmmss-<6 hex>). Obtain it once per chat and pass it on every call; a
        transitional summary names it so the next chat continues the session (cwd, environment, jobs)."""
        return await anyio.to_thread.run_sync(execution.get_session_id, rt)

    @mcp.tool(name="getSession", structured_output=False)
    async def get_session(sessionId: str) -> Reply:
        """The session's working directory, persisted environment names, foreground call and jobs. A valid id never
        seen before creates the session."""
        return await anyio.to_thread.run_sync(execution.get_session, rt, sessionId)

    @mcp.tool(name="executeCommand", structured_output=False)
    async def execute_command(sessionId: str, command: str, cwd: str | None = None,
                              timeoutSeconds: int | None = None, maxLines: int | None = None) -> Reply:
        """Runs PowerShell text (one statement, a ; or && / || chain, a pipeline, a multi-line script) in a fresh
        pwsh in the session's working directory and environment; cd and $env: changes persist in the session.
        `cwd` applies to this call only. Waits up to timeoutSeconds; a command still running then continues as a job
        (state "running", jobId) and is never killed. Content 0: JSON header (exitCode, cwd, lines, logPath, ...);
        content 1: the output, head and tail kept within maxLines."""
        return await execution.execute_command(rt, sessionId, command, cwd, timeoutSeconds, maxLines)

    install_call_handler(mcp, rt)
    return mcp


def install_call_handler(mcp: FastMCP, rt: Runtime) -> None:
    tool_manager = mcp._tool_manager  # FastMCP 1.12 keeps its registry here; guarded by tests/test_server_stdio.py

    async def handle(request: types.CallToolRequest) -> types.ServerResult:
        at = datetime.now().astimezone()
        started = time.monotonic()
        name = request.params.name
        arguments: dict[str, Any] = dict(request.params.arguments or {})
        reply = await _run_tool(tool_manager, name, arguments)
        seconds = time.monotonic() - started
        secrets = await anyio.to_thread.run_sync(execution.secret_values, rt)
        header_text = mask(json.dumps(reply.header, ensure_ascii=False), secrets)
        text = mask(reply.text, secrets) if reply.text else None
        session_id = reply.session_id
        if session_id is None and is_valid_session_id(arguments.get("sessionId")):
            session_id = arguments["sessionId"]
        if session_id is not None:
            block = transcript.format_block(at, reply.call_id, name, arguments, reply.header, seconds, text=reply.text)
            try:
                await transcript.append(rt.home, session_id, mask(block, secrets))
            except Exception:
                log.exception("transcript append failed for session %s", session_id)
        contents = [types.TextContent(type="text", text=header_text)]
        if text:
            contents.append(types.TextContent(type="text", text=text))
        return types.ServerResult(types.CallToolResult(content=contents, isError=reply.is_error))

    mcp._mcp_server.request_handlers[types.CallToolRequest] = handle


async def _run_tool(tool_manager, name: str, arguments: dict[str, Any]) -> Reply:
    tool = tool_manager.get_tool(name)
    if tool is None:
        return refusal("UNKNOWN_TOOL", tool=name)
    try:
        result = await tool.run(arguments)
    except ToolError as e:
        cause = e.__cause__
        if isinstance(cause, pydantic.ValidationError):
            return refusal("BAD_ARGUMENTS", message=_validation_message(cause))
        log.error("tool %s failed", name, exc_info=cause or e)
        return refusal("INTERNAL_ERROR", message=f"{type(cause or e).__name__}: {cause or e}")
    if not isinstance(result, Reply):
        log.error("tool %s returned %r", name, type(result))
        return refusal("INTERNAL_ERROR", message=f"tool returned {type(result).__name__}")
    return result


def _validation_message(error: pydantic.ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}" for item in error.errors())
