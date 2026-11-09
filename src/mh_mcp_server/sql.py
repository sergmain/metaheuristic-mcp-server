"""sqlQuery (0.1.12): a query on the dev PostgreSQL through psql, read only, without pwsh.

mh-mcp's transcripts (temp/mh-mcp-usage-2.py): 243 psql commands in 2026-10-23 - 10-26 and 726 in 10-27 - 11-09, 56 - 61
a day, each one a pwsh process (4.9 - 6.7 s per command on average) with the SQL quoted inline for pwsh.

How a query runs - each point probed on psql 18.4 on 2026-11-09:
- The SQL goes to psql on stdin, as UTF-8 with PGCLIENTENCODING=UTF8. A `-c` argument would lose every character
  outside the ANSI code page: psql reads its argv in that code page on Windows ('б' arrived as '?').
- The first stdin line is `\\restrict <key>`, with a random key per call. psql then refuses every backslash command except
  `\\unrestrict <key>` - on a line of its own or in the middle of one - so the SQL can neither run `\\!` (a shell) nor
  write files past the server's roots with `\\o` or `\\copy ... to`. This is pg_dump's own guard, and psql has had it since
  17.6 / 18. A backslash inside a string literal (a regex such as '[^\\s]+') is SQL and still works.
- The SQL is read only twice over. The role in [sql] user is meant to hold SELECT only (pg_read_all_data), and that is the
  guard SQL text cannot lift. PGOPTIONS also sets default_transaction_read_only, which SQL text can lift
  (`commit; set default_transaction_read_only = off`). That setting only makes an ordinary write fail before it reaches
  the role's refusal. statement_timeout ends a query a few seconds before the call's deadline.
- The password is the value of the command environment's variable [sql] passwordVariable (config [env]; [secrets] masks
  it). Every other PG* variable is dropped, so nothing in the registry or the session's environment re-routes the
  connection, and a database is a plain name, never a connection string.
- psql writes its whole result into the call's output.csv (`logPath`, readable with readFile). The reply shows the
  columns line and the first maxRows rows, within readFile's byte budget. NULL shows as <NULL>, which tells it apart from
  an empty string. Several statements print one result after another, so a second result's columns line counts as a row.
"""

from __future__ import annotations

import csv
import io
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from mh_mcp_server.calls import allocate_call, runs_dir
from mh_mcp_server.children import env_value, run_child, without
from mh_mcp_server.config import Config
from mh_mcp_server.execution import Reply, Runtime, command_env, refusal
from mh_mcp_server.session_id import is_valid_session_id
from mh_mcp_server.session_state import load_state

ROWS_DEFAULT = 200
ROWS_MAX = 5000
NULL = "<NULL>"
DATABASE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,62}$")
STDERR_CHARS = 4000
CONNECT_SECONDS = 10
STATEMENT_MARGIN_SECONDS = 3


# ==================== pure ====================

def restricted_script(sql: str, key: str) -> bytes:
    """What psql reads on stdin: restricted mode under `key`, then the SQL, ended by a newline."""
    return f"\\restrict {key}\n{sql}\n".encode("utf-8")


def psql_argv(psql: str, config: Config, database: str) -> list[str]:
    """No psqlrc (-X), never a password prompt (-w), no command tags (-q), CSV, a visible NULL, stop at the first
    error (exit 3)."""
    return [psql, "-X", "-w", "-q", "-h", config.sql_host, "-p", str(config.sql_port), "-U", config.sql_user,
            "-d", database, "--csv", "-P", f"null={NULL}", "-v", "ON_ERROR_STOP=1"]


def statement_timeout_ms(timeout_seconds: int) -> int:
    return max(1, timeout_seconds - STATEMENT_MARGIN_SECONDS) * 1000


def psql_env(env: dict[str, str], password: str, timeout_seconds: int) -> dict[str, str]:
    """The command environment without any PG* variable, plus the connection's own."""
    return {**without(env, lambda name: name.casefold().startswith("pg")),
            "PGPASSWORD": password, "PGCLIENTENCODING": "UTF8", "PGCONNECT_TIMEOUT": str(CONNECT_SECONDS),
            "PGAPPNAME": "mh-mcp sqlQuery",
            "PGOPTIONS": f"-c default_transaction_read_only=on -c statement_timeout={statement_timeout_ms(timeout_seconds)}"}


def shown_rows(csv_text: str, max_rows: int, max_bytes: int) -> tuple[list[str], int, int, bool]:
    """(lines shown, data rows in all, data rows shown, truncated) of psql's CSV: the first record is the columns line,
    always shown; then rows until max_rows or max_bytes. A value's CRLF (psql's Windows newline) is shown as LF."""
    lines: list[str] = []
    used = rows = returned = 0
    truncated = False
    for index, record in enumerate(csv.reader(io.StringIO(csv_text, newline=""))):
        if index > 0:
            rows += 1
            if truncated or returned >= max_rows:
                truncated = True
                continue
        line = _csv_line([value.replace("\r\n", "\n") for value in record])
        used += len(line.encode("utf-8")) + 1
        if index > 0 and used > max_bytes:
            truncated = True
            continue
        lines.append(line)
        if index > 0:
            returned += 1
    return lines, rows, returned, truncated


def _csv_line(values: list[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(values)
    return buffer.getvalue()


# ==================== the tool ====================

def sql_query(rt: Runtime, session_id: str, sql: str, database: str | None = None, max_rows: int | None = None,
              timeout_seconds: int | None = None) -> Reply:
    if not is_valid_session_id(session_id):
        return refusal("BAD_SESSION_ID")
    timeout = rt.config.default_foreground_seconds if timeout_seconds is None else timeout_seconds
    if timeout > rt.config.max_foreground_seconds:
        return refusal("TIMEOUT_TOO_LARGE", session_id, max=rt.config.max_foreground_seconds)
    limit = ROWS_DEFAULT if max_rows is None else max_rows
    if not sql.strip() or timeout < 1 or not 1 <= limit <= ROWS_MAX:
        return refusal("BAD_ARGUMENTS", session_id,
                       message=f"sql must not be blank, timeoutSeconds >= 1, maxRows 1 - {ROWS_MAX}")
    name = rt.config.sql_database if database is None else database
    if not DATABASE.match(name):
        return refusal("BAD_ARGUMENTS", session_id,
                       message="database: a plain name (letters, digits, _ and -), never a connection string")
    call_id, call_dir = allocate_call(runs_dir(rt.home, session_id))
    env = command_env(rt, session_id, call_id, load_state(rt.home, session_id, rt.config.default_cwd))
    password = env_value(env, rt.config.sql_password_variable)
    psql = shutil.which(rt.config.sql_psql, path=env_value(env, "PATH"))
    if not password or psql is None:
        call_dir.rmdir()
        return refusal("SQL_UNAVAILABLE", session_id, message=(
            f"no password: the variable {rt.config.sql_password_variable} is not set (config [env])" if not password
            else f"{rt.config.sql_psql} not found on the command PATH"))
    started = time.monotonic()
    output = call_dir / "output.csv"
    finished = run_child(psql_argv(psql, rt.config, name), psql_env(env, password, timeout), call_dir,
                         restricted_script(sql, secrets.token_hex(16)), timeout, output)
    lines, rows, returned, truncated = shown_rows(output.read_bytes().decode("utf-8", "replace"), limit,
                                                  rt.config.read_max_bytes)
    header: dict[str, Any] = {"callId": call_id, "exitCode": finished.exit_code, "database": name, "rows": rows,
                              "returned": returned, "seconds": round(time.monotonic() - started, 2),
                              "logPath": str(output)}
    if truncated:
        header["truncated"] = True
    if finished.timed_out:
        header["timedOut"] = True
    stderr = finished.stderr.decode("utf-8", "replace").replace("\r\n", "\n").strip()
    if stderr:
        header["stderr"] = stderr[:STDERR_CHARS]
    return Reply(header, "\n".join(lines) or None, False, call_id, session_id)
