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

from mh_mcp_server import execution, files, jobs, transcript
from mh_mcp_server.environment import mask
from mh_mcp_server.execution import Reply, Runtime, refusal
from mh_mcp_server.session_id import is_valid_session_id

log = logging.getLogger("mh_mcp_server")

TOOLS_PHASE_7 = ("getSessionId", "getSession", "executeCommand")
TOOLS_PHASE_8 = TOOLS_PHASE_7 + ("startJob", "waitJob")
TOOLS = TOOLS_PHASE_8 + ("readFile", "writeFile", "editFile")


class Edit(pydantic.BaseModel):
    old: str
    new: str
    count: int = 1


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

    @mcp.tool(name="startJob", structured_output=False)
    async def start_job(sessionId: str, command: str, cwd: str | None = None, name: str | None = None) -> Reply:
        """Starts PowerShell text as a background job in the session's working directory and environment and
        returns at once with its jobId and logPath. A job never blocks the session and never changes its cwd or
        environment. Use waitJob to follow it; it is never killed."""
        return await anyio.to_thread.run_sync(jobs.start_job, rt, sessionId, command, cwd, name)

    @mcp.tool(name="waitJob", structured_output=False)
    async def wait_job(sessionId: str, jobId: str, maxSeconds: int | None = None, sinceLine: int = 0,
                       untilRegex: str | None = None, maxLines: int | None = None) -> Reply:
        """Waits on a job until it is done (or lost), a line after sinceLine matches untilRegex (then `matched`), or
        maxSeconds pass. Returns its state, exitCode when done, and the lines after sinceLine (the tail kept within
        maxLines); pass the returned nextLine back as sinceLine so no line is returned twice."""
        return await jobs.wait_job(rt, sessionId, jobId, maxSeconds, sinceLine, untilRegex, maxLines)

    @mcp.tool(name="readFile", structured_output=False)
    async def read_file(sessionId: str, path: str, fromLine: int = 1, toLine: int | None = None,
                        maxLines: int | None = None) -> Reply:
        """Reads a text file as `N: <line>` lines. totalLines is always the file's real line count; fromLine < 0
        means the last |fromLine| lines. A read stops at the configured line/byte budget on a line boundary and
        returns nextFromLine - continue from it; nothing in the middle is ever skipped. A relative path is relative
        to the session's working directory."""
        return await anyio.to_thread.run_sync(files.read_file, rt, sessionId, path, fromLine, toLine, maxLines)

    @mcp.tool(name="writeFile", structured_output=False)
    async def write_file(sessionId: str, path: str, content: str, mode: str = "create",
                         lineEnding: str | None = None) -> Reply:
        """Writes a whole file, UTF-8. mode "create" refuses an existing file, "overwrite" replaces it. content's
        CRLF is normalized to LF, then written with lineEnding ("lf" / "crlf"; default: the existing file's ending,
        or lf for a new file). A BOM is kept when the file had one. Parent directories are created."""
        return await anyio.to_thread.run_sync(files.write_file, rt, sessionId, path, content, mode, lineEnding)

    @mcp.tool(name="editFile", structured_output=False)
    async def edit_file(sessionId: str, path: str, edits: list[Edit], dryRun: bool = False) -> Reply:
        """Exact replacements, applied in order; each `old` must occur exactly `count` times (default 1) in the text
        the previous edit left, or nothing is written. old/new are written with LF; on a CRLF file they are
        converted to CRLF. Line endings and every byte outside the replaced text are kept; a file with mixed line
        endings is refused. Returns per edit the 1-based line where its first replacement starts."""
        return await anyio.to_thread.run_sync(files.edit_file, rt, sessionId, path,
                                              [e.model_dump() for e in edits], dryRun)

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
