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
from typing import Any, Callable

import anyio.to_thread
import pydantic
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.stdio import stdio_server

from mh_mcp_server import execution, files, finder, jobs, memory, transcript
from mh_mcp_server import semaphores
from mh_mcp_server import __version__
from mh_mcp_server.environment import mask
from mh_mcp_server.execution import Reply, Runtime, refusal
from mh_mcp_server.session_id import is_valid_session_id
from mh_mcp_server.sql import sql_query as run_sql_query
from mh_mcp_server.gitcommit import git_commit as run_git_commit
from mh_mcp_server.jobs import wait_job as run_wait_job

log = logging.getLogger("mh_mcp_server")
calls_log = logging.getLogger("mh_mcp_server.calls")   # file only (not stderr): wired by __main__.setup_logging


def is_cancellation(message: Any) -> bool:
    """True for an incoming `notifications/cancelled` (a SessionMessage from the stdio reader)."""
    root = getattr(getattr(message, "message", None), "root", None)
    return isinstance(root, types.JSONRPCNotification) and root.method == "notifications/cancelled"


def request_id_of(mcp: FastMCP) -> Any:
    """The JSON-RPC id of the request being handled, for the calls log; None outside a request."""
    try:
        return mcp._mcp_server.request_context.request_id
    except LookupError:
        return None


def logged_session_ids(arguments: dict[str, Any]) -> Any:
    """The sessionId a call names for the calls log - for waitJob, its jobs' sessionIds joined by commas."""
    if "sessionId" in arguments:
        return arguments["sessionId"]
    refs = arguments.get("jobs")
    if isinstance(refs, list):
        return ",".join(dict.fromkeys(str(r.get("sessionId")) for r in refs if isinstance(r, dict))) or None
    return None

TOOLS_PHASE_7 = ("getSessionId", "getSession", "executeCommand")
TOOLS_PHASE_8 = TOOLS_PHASE_7 + ("startJob", "waitJob")
TOOLS = TOOLS_PHASE_8 + ("readFile", "writeFile", "editFile")
TOOLS_0_1_9 = TOOLS + ("getMemory",)
TOOLS_0_1_10 = TOOLS_0_1_9 + ("searchFiles", "listFiles", "fileInfo")
TOOLS_0_1_11 = TOOLS_0_1_10 + ("cancelJob",)
TOOLS_0_1_12 = TOOLS_0_1_11 + ("sqlQuery", "gitCommit")
# 0.1.13 (plan 056): no new tool - waitJob over several jobs, searchFiles over several roots with files / count modes,
# readFile over several ranges; gitInspect not built (Phase 5 no-go, 2026-11-12)
TOOLS_0_1_13 = TOOLS_0_1_12
# 0.1.14: no new tool - waitJob's reply carries getMemory's reading taken when the wait ended
TOOLS_0_1_14 = TOOLS_0_1_13
# 0.1.15: semaphores over shared build resources - registered only while config [semaphores] enabled
SEMAPHORE_TOOLS = ("acquireSemaphore", "releaseSemaphore", "listSemaphores")
TOOLS_0_1_15 = TOOLS_0_1_14 + SEMAPHORE_TOOLS


class Edit(pydantic.BaseModel):
    old: str
    new: str
    count: int = 1


class JobRef(pydantic.BaseModel):
    """One job of a waitJob call, named by its (sessionId, jobId) pair, with its own line cursor."""
    sessionId: str
    jobId: str
    sinceLine: int = 0


class Range(pydantic.BaseModel):
    """One slice of a readFile call over several ranges."""
    path: str
    fromLine: int = 1
    toLine: int | None = None


async def _semaphore_step(step: Callable[..., Any], *args: Any) -> Any:
    """A semaphore step beside a tool call (0.1.15): an error in it is logged and never the call's failure - the
    semaphores are a guard, the call is the work."""
    try:
        return await anyio.to_thread.run_sync(step, *args)
    except Exception:
        log.exception("semaphore step %s failed", getattr(step, "__name__", step))
        return None


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
                              timeoutSeconds: int | None = None, maxLines: int | None = None,
                              semaphore: str | None = None) -> Reply:
        """Runs PowerShell text (one statement, a ; or && / || chain, a pipeline, a multi-line script) in a fresh
        pwsh in the session's working directory and environment; cd and $env: changes persist in the session.
        `cwd` applies to this call only. Waits up to timeoutSeconds; a command still running then continues as a job
        (state "running", jobId) and is never killed. Content 0: JSON header (exitCode, cwd, lines, logPath, ...);
        content 1: the output, head and tail kept within maxLines. `semaphore`: the id acquireSemaphore returned -
        refreshes it, and a job the call leaves running joins it; an `mvn` command reaching a resource another
        semaphore holds is refused SEMAPHORE_HELD (ignored while semaphores are disabled)."""
        if rt.config.semaphores_enabled:
            refused = await _semaphore_step(semaphores.gate, rt, sessionId, command, cwd, semaphore)
            if refused is not None:
                return refused
        reply = await execution.execute_command(rt, sessionId, command, cwd, timeoutSeconds, maxLines)
        if rt.config.semaphores_enabled and semaphore is not None:
            await _semaphore_step(semaphores.joined, rt, semaphore, sessionId, reply)
        return reply

    @mcp.tool(name="startJob", structured_output=False)
    async def start_job(sessionId: str, command: str, cwd: str | None = None, name: str | None = None,
                        semaphore: str | None = None) -> Reply:
        """Starts PowerShell text as a background job in the session's working directory and environment and
        returns at once with its jobId and logPath. A job never blocks the session and never changes its cwd or
        environment. Use waitJob to follow it; it is never killed. `semaphore`: the id acquireSemaphore returned -
        refreshes it and the job joins it (a waitJob on the job then refreshes it too); an `mvn` command reaching a
        resource another semaphore holds is refused SEMAPHORE_HELD (ignored while semaphores are disabled)."""
        if rt.config.semaphores_enabled:
            refused = await _semaphore_step(semaphores.gate, rt, sessionId, command, cwd, semaphore)
            if refused is not None:
                return refused
        reply = await anyio.to_thread.run_sync(jobs.start_job, rt, sessionId, command, cwd, name)
        if rt.config.semaphores_enabled and semaphore is not None:
            await _semaphore_step(semaphores.joined, rt, semaphore, sessionId, reply)
        return reply

    @mcp.tool(name="waitJob", structured_output=False)
    async def wait_job(jobs: list[JobRef], mode: str = "any", maxSeconds: int | None = None,
                       untilRegex: str | None = None, maxLines: int | None = None) -> Reply:
        """Waits on 1-20 jobs in one call, each named by its sessionId and jobId (jobs of several sessions
        may be mixed) with its own sinceLine. mode "any" (default): returns when any job is done or lost, or a line of
        any job after its sinceLine matches untilRegex; mode "all": when every job is done or lost - a match settles
        that job without ending the wait. Either returns at maxSeconds. Header: mode, seconds, and per job sessionId,
        jobId, state, exitCode when done, cancelled, fromLine, nextLine (pass it back as that job's sinceLine so no line
        is returned twice), matched, omitted - or error JOB_NOT_FOUND for an unknown job, the others answered; and
        memory - getMemory's reading taken when the wait ended (usedGB, availableGB, totalGB, memoryLoad, commitUsedGB,
        commitFreeGB, commitLimitGB), to decide what to start next. Content 1: per job with new lines,
        `== <sessionId>/<jobId> <state>` then its tail (maxLines per job). A semaphore a waited job belongs to is
        refreshed."""
        reply = await run_wait_job(rt, [j.model_dump() for j in jobs], mode, maxSeconds, untilRegex, maxLines)
        if rt.config.semaphores_enabled and not reply.is_error:
            await _semaphore_step(semaphores.touch_jobs, rt, [(j.sessionId, j.jobId) for j in jobs])
        return reply

    if rt.config.semaphores_enabled:
        @mcp.tool(name="acquireSemaphore", structured_output=False)
        async def acquire_semaphore(sessionId: str, type: str, path: str, ttlSeconds: int | None = None,
                                    waitSeconds: int = 0) -> Reply:
            """Acquires the semaphore of the protected resource `path` belongs to (type MAVEN: the Maven tree a build
            there writes), shared by every chat. Acquired: {acquired: true, semaphore: <id>, resource, ttlSeconds} -
            pass the id to startJob / executeCommand for every run on the resource, releaseSemaphore it when done.
            Held by another chat: {acquired: false, resource, heldBy: {sessionId, touchedAt, expiresInSeconds, jobs}}
            - not an error: call again, or give waitSeconds (0-58) to wait for it in this call. A semaphore is gone
            after ttlSeconds (default 600, max 3600) without use (acquire, a call with its id, a waitJob on one of its
            jobs) or when the mh-mcp process that created it ends. A path outside every protected resource is refused
            NOT_PROTECTED: no semaphore is needed there."""
            return await semaphores.acquire(rt, sessionId, type, path, ttlSeconds, waitSeconds)

        @mcp.tool(name="releaseSemaphore", structured_output=False)
        async def release_semaphore(sessionId: str, semaphore: str) -> Reply:
            """Releases the semaphore with this id. Header: released, type, resource, jobs (their states - nothing is
            killed). An unknown, released or expired id is refused SEMAPHORE_NOT_FOUND."""
            return await anyio.to_thread.run_sync(semaphores.release, rt, sessionId, semaphore)

        @mcp.tool(name="listSemaphores", structured_output=False)
        async def list_semaphores(sessionId: str) -> Reply:
            """Every live semaphore - type, resource, sessionId that acquired it, acquiredAt, touchedAt,
            expiresInSeconds, jobs with their states; its id only to the session that acquired it - and the protected
            resources: MAVEN dirs (each one resource) and multiApp roots (each directory with a pom.xml or
            pom-commons.xml under them is one)."""
            return await anyio.to_thread.run_sync(semaphores.list_semaphores, rt, sessionId)

    @mcp.tool(name="readFile", structured_output=False)
    async def read_file(sessionId: str, path: str | None = None, fromLine: int = 1, toLine: int | None = None,
                        maxLines: int | None = None, ranges: list[Range] | None = None) -> Reply:
        """Reads a text file as `N: <line>` lines. totalLines is always the file's real line count; fromLine < 0
        means the last |fromLine| lines. A read stops at the configured line/byte budget on a line boundary and
        returns nextFromLine - continue from it; nothing in the middle is ever skipped. A relative path is relative
        to the session's working directory. `ranges` - 1-20 {path, fromLine, toLine}, given instead of path /
        fromLine / toLine - reads several slices of one or more files in one call: the budget is shared, ranges taken
        in order; a range the budget cut gets nextFromLine, one it never reached has nextFromLine = its fromLine; a
        refused or missing path is its entry's error, the other ranges are read. Header then {"ranges": [...]} with a
        single read's header per range; content 1 per range read `== <path> <fromLine>-<toLine>` and its lines."""
        return await anyio.to_thread.run_sync(files.read_file, rt, sessionId, path, fromLine, toLine, maxLines,
                                              None if ranges is None else [r.model_dump() for r in ranges])

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

    @mcp.tool(name="getMemory", structured_output=False)
    async def get_memory(sessionId: str) -> Reply:
        """The box's memory in one in-process call (no pwsh): usedGB, availableGB, totalGB, memoryLoad (percent of
        physical memory in use), commitUsedGB, commitFreeGB, commitLimitGB - GiB, one decimal. Use it instead of a
        command querying WMI."""
        return await anyio.to_thread.run_sync(memory.get_memory, sessionId)

    @mcp.tool(name="searchFiles", structured_output=False)
    async def search_files(sessionId: str, roots: list[str], regex: str, glob: str | None = None,
                           outputMode: str = "lines", maxResults: int | None = None, context: int = 0,
                           ignoreCase: bool = False, includeSkipped: bool = False) -> Reply:
        """Searches the text files under 1-20 `roots` (each a directory, walked recursively, or one file) for a Python
        regex, in process - no pwsh. Paths are relative to `base`, the deepest directory common to all roots; a file
        reached through two roots is searched once. outputMode "lines" (default): `path:line: text` per match (context
        lines `path:line- text`); "files": one path per file with a match; "count": `path: N` per file with matches -
        maxResults counts files in both, and context needs "lines". `glob` filters file names (comma-separated, e.g.
        "*.java,*.py"); .git, target, node_modules, __pycache__, .idea, .venv, dist, .pytest_cache are skipped unless
        includeSkipped; binary and > 64 MB files are skipped and counted. A refused root refuses the call. Header: base,
        matches, files, scanned, truncated when maxResults (default 200) or the byte budget stopped it."""
        return await anyio.to_thread.run_sync(finder.search_files, rt, sessionId, roots, regex, glob, outputMode,
                                              maxResults, context, ignoreCase, includeSkipped)

    @mcp.tool(name="listFiles", structured_output=False)
    async def list_files(sessionId: str, root: str, glob: str | None = None, maxDepth: int = 1,
                         maxResults: int | None = None, includeSkipped: bool = False) -> Reply:
        """Lists `root` in process - no pwsh: one line per entry, `d|f  bytes  modified  path` (path relative to root),
        down to maxDepth (1 = the root's own entries). With `glob` (comma-separated name patterns) only matching files are
        listed - a recursive find with maxDepth > 1. Skips as searchFiles does. Header: dirs, files, truncated."""
        return await anyio.to_thread.run_sync(finder.list_files, rt, sessionId, root, glob, maxDepth, maxResults,
                                              includeSkipped)

    @mcp.tool(name="fileInfo", structured_output=False)
    async def file_info(sessionId: str, paths: list[str]) -> Reply:
        """For up to 50 files at once, in process: lines (readFile's count), bytes, lineEnding, bom, binary, modified.
        A path that is refused or missing gets its error in its own entry; the call itself never fails for one path."""
        return await anyio.to_thread.run_sync(finder.file_info, rt, sessionId, paths)

    @mcp.tool(name="cancelJob", structured_output=False)
    async def cancel_job(sessionId: str, jobId: str) -> Reply:
        """Cancels a running job - or an executeCommand that outlived its wait and became one: kills its pwsh and every
        process it started (only while that pwsh is still the process the job recorded). The job then ends `done` with
        the killed shell's exit code; the header and every later waitJob carry `cancelled: true`. A job no longer running
        is reported as it is, nothing killed. Header: jobId, state, cancelled, killed, exitCode."""
        return await anyio.to_thread.run_sync(jobs.cancel_job, rt, sessionId, jobId)

    @mcp.tool(name="sqlQuery", structured_output=False)
    async def sql_query(sessionId: str, sql: str, database: str | None = None, maxRows: int | None = None,
                        timeoutSeconds: int | None = None) -> Reply:
        """Runs SQL on the dev PostgreSQL through psql, read only, no pwsh: the SQL goes to psql on stdin as UTF-8 in
        psql's restricted mode (no backslash command runs), as config [sql]'s SELECT-only role, with
        default_transaction_read_only and a statement_timeout before the call's deadline. `database` is a plain name
        (default: config [sql] database). Content 0: callId, exitCode (psql's: 0 ok, 2 no connection, 3 an SQL error,
        its text in `stderr`), rows, returned, truncated, timedOut, logPath (the whole result as CSV); content 1: the
        columns line and the first maxRows (default 200) rows as CSV, NULL shown as <NULL>. One statement per call: a
        second one's result follows the first's."""
        return await anyio.to_thread.run_sync(run_sql_query, rt, sessionId, sql, database, maxRows, timeoutSeconds)

    @mcp.tool(name="gitCommit", structured_output=False)
    async def git_commit(sessionId: str, repo: str, paths: list[str], message: str, push: bool = True,
                         timeoutSeconds: int | None = None) -> Reply:
        """Commits exactly `paths` of the git repository at `repo`, then pushes - git run directly, no pwsh, nothing
        quoted: `git add -- <paths>` (new, modified and deleted files), `git commit -F - -- <paths>` (the message on stdin;
        another path's staged change stays out of the commit and stays staged), then `git push origin HEAD` unless push
        is false. A path names one file, or one deleted since HEAD, under the repository's top level - never the
        repository itself, a directory or a wildcard. Header: repo, branch, commit, files, pushed, exitCode (0 when every
        step succeeded), step (the step that failed), timedOut; content 1: the commit's name-status and git's push
        output, or the failed step's output."""
        return await anyio.to_thread.run_sync(run_git_commit, rt, sessionId, repo, paths, message, push, timeoutSeconds)

    install_call_handler(mcp, rt)
    return mcp


def install_call_handler(mcp: FastMCP, rt: Runtime) -> None:
    tool_manager = mcp._tool_manager  # FastMCP 1.12 keeps its registry here; guarded by tests/test_server_stdio.py

    async def handle(request: types.CallToolRequest) -> types.ServerResult:
        at = datetime.now().astimezone()
        started = time.monotonic()
        name = request.params.name
        arguments: dict[str, Any] = dict(request.params.arguments or {})
        request_id = request_id_of(mcp)
        calls_log.info("request %s: %s sessionId=%s arrived", request_id, name, logged_session_ids(arguments))
        reply = await _run_tool(tool_manager, name, arguments)
        seconds = time.monotonic() - started
        calls_log.info("request %s: %s done in %.2f s", request_id, name, seconds)
        secrets = await anyio.to_thread.run_sync(execution.secret_values, rt)
        header_text = mask(json.dumps(reply.header, ensure_ascii=False), secrets)
        text = mask(reply.text, secrets) if reply.text else None
        session_id = reply.session_id
        if session_id is None and is_valid_session_id(arguments.get("sessionId")):
            session_id = arguments["sessionId"]
        # waitJob over several sessions: the same block goes to every session the call names
        targets = list(dict.fromkeys(s for s in (session_id, *reply.session_ids) if s is not None))
        if targets:
            nbytes = len(header_text.encode("utf-8")) + (len(text.encode("utf-8")) if text else 0)
            block = mask(transcript.format_block(at, reply.call_id, name, arguments, reply.header, seconds,
                                                 text=reply.text, nbytes=nbytes, version=__version__), secrets)
            for target in targets:
                try:
                    await transcript.append(rt.home, target, block)
                except Exception:
                    log.exception("transcript append failed for session %s", target)
        contents = [types.TextContent(type="text", text=header_text)]
        if text:
            contents.append(types.TextContent(type="text", text=text))
        return types.ServerResult(types.CallToolResult(content=contents, isError=reply.is_error))

    mcp._mcp_server.request_handlers[types.CallToolRequest] = handle


async def serve_stdio(mcp: FastMCP) -> None:
    """FastMCP's stdio loop, except that the end of stdin - the client is gone - also ends the calls in flight.

    The SDK's loop stops reading at EOF and then waits for every running handler, so a server whose client had died
    lived on until its longest call returned (up to the 58 s wait limit). A call cut short here loses nothing: its
    command runs under its own supervisor and is a job from then on (no delivered.json), and the foreground lock of a
    server that has exited is stale.

    `notifications/cancelled` is dropped here, before the SDK sees it (2026-10-24): in mcp 1.12.1 a cancellation that
    lands while a handler awaits leaks a CancelledError out of RequestResponder.__exit__ and ends the task group around
    the receive loop, so the instance never answers again (modelcontextprotocol/python-sdk#2610, reproduced by
    tests/test_cancelled_request.py). Nothing is lost by ignoring it - the spec lets a receiver ignore a request that
    cannot be cancelled, and none here can: a command is never killed and every call returns within 58 s; the client
    ignores the late result."""
    server = mcp._mcp_server
    async with stdio_server() as (read_stream, write_stream):
        forward_send, forward_receive = anyio.create_memory_object_stream(0)
        async with anyio.create_task_group() as tg:
            async def forward() -> None:
                async with forward_send:
                    async for message in read_stream:
                        if is_cancellation(message):
                            params = message.message.root.params or {}
                            calls_log.info("request %s: notifications/cancelled ignored", params.get("requestId"))
                            continue
                        await forward_send.send(message)
                tg.cancel_scope.cancel()

            tg.start_soon(forward)
            await server.run(forward_receive, write_stream, server.create_initialization_options())


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
