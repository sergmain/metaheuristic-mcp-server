"""sqlQuery (0.1.12) against the box's real PostgreSQL - no mock of psql or of the server.

Each run makes a throwaway SELECT-only role (pg_read_all_data) and a two-row table in the `postgres` database, and drops
both at the end, as the superuser whose password the test process has from mh-mcp's own [env] (PGPASSWORD). The role may
use the reserved connection slots (pg_use_reserved_connections), like mh_mcp_ro, so it connects while MH's pool holds
every ordinary slot. Without psql or that password the database tests are skipped; the pure parts and the refusals run
anywhere.
"""

import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

from mh_mcp_server.config import load_config, read_text_if_exists
from mh_mcp_server.execution import production_runtime
from mh_mcp_server.session_state import SessionState, save_state
from mh_mcp_server.sql import restricted_script, shown_rows, sql_query, statement_timeout_ms

SID = "20261109-120000-0001a2"
PSQL = shutil.which("psql")
SUPERUSER_PASSWORD = os.environ.get("PGPASSWORD")
DB = "postgres"
needs_pg = pytest.mark.skipif(PSQL is None or not SUPERUSER_PASSWORD, reason="no psql or no PGPASSWORD")


def superuser(sql: str) -> str:
    result = subprocess.run([PSQL, "-X", "-w", "-q", "-At", "-h", "localhost", "-U", "postgres", "-d", DB,
                             "-v", "ON_ERROR_STOP=1"], input=sql.encode("utf-8"), capture_output=True, timeout=60,
                            env={**os.environ, "PGCLIENTENCODING": "UTF8"})
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout.decode("utf-8").strip()


@pytest.fixture(scope="module")
def reader():
    suffix = secrets.token_hex(4)
    role, table, password = f"mh_mcp_test_ro_{suffix}", f"mh_mcp_test_t_{suffix}", secrets.token_hex(16)
    superuser(f"create role {role} login password '{password}'; grant pg_read_all_data to {role}; "
              f"grant pg_use_reserved_connections to {role}; "
              f"create table public.{table} (v int); insert into public.{table} values (1), (2);")
    try:
        yield role, table, password
    finally:
        superuser(f"drop table if exists public.{table}; drop role if exists {role};")


def runtime(tmp_path: Path, role: str = "nobody", password: str = "pw", **sql_keys: str):
    """A home whose [sql] connects as `role` to `postgres`, its password in [env] (and [secrets])."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    keys = {"user": role, "database": DB, "passwordVariable": "MCP_TEST_SQL_PW", **sql_keys}
    (home / "config.toml").write_text(
        "[execution]\n"
        f'defaultCwd = "{tmp_path.as_posix()}"\n'
        "[env]\n"
        f'MCP_TEST_SQL_PW = "{password}"\n'
        "[secrets]\n"
        'names = ["MCP_TEST_SQL_PW"]\n'
        "[sql]\n" + "".join(f'{key} = "{value}"\n' for key, value in keys.items()), encoding="utf-8")
    return production_runtime(home, load_config(home, read_text_if_exists))


# ==================== pure ====================

def test_the_script_enters_restricted_mode_before_the_sql():
    assert restricted_script("select 'б'", "abc123") == "\\restrict abc123\nselect 'б'\n".encode("utf-8")


def test_the_statement_timeout_leaves_a_margin_before_the_deadline():
    assert (statement_timeout_ms(55), statement_timeout_ms(4), statement_timeout_ms(1)) == (52000, 1000, 1000)


def test_shown_rows_keeps_the_columns_line_and_cuts_at_max_rows():
    csv_text = "g\r\n1\r\n2\r\n3\r\n4\r\n"
    assert shown_rows(csv_text, 2, 10_000) == (["g", "1", "2"], 4, 2, True)
    assert shown_rows(csv_text, 4, 10_000) == (["g", "1", "2", "3", "4"], 4, 4, False)


def test_shown_rows_cuts_at_the_byte_budget_but_counts_every_row():
    lines, rows, returned, truncated = shown_rows("v\r\n" + "".join(f"{'x' * 10}{n}\r\n" for n in range(9)), 100, 30)
    assert (lines, rows, returned, truncated) == (["v", "xxxxxxxxxx0", "xxxxxxxxxx1"], 9, 2, True)


def test_shown_rows_quotes_as_csv_and_shows_a_value_s_crlf_as_lf():
    lines, rows, returned, truncated = shown_rows('a,b\r\n"l1\r\nl2","x,""y"""\r\n', 10, 10_000)
    assert lines == ["a,b", '"l1\nl2","x,""y"""']
    assert (rows, returned, truncated) == (1, 1, False)


def test_shown_rows_of_no_output_is_empty():
    assert shown_rows("", 10, 10_000) == ([], 0, 0, False)


# ==================== refusals - nothing reaches psql ====================

@pytest.mark.parametrize("arguments, error", [
    ({"sql": "  "}, "BAD_ARGUMENTS"),
    ({"sql": "select 1", "max_rows": 0}, "BAD_ARGUMENTS"),
    ({"sql": "select 1", "max_rows": 5001}, "BAD_ARGUMENTS"),
    ({"sql": "select 1", "timeout_seconds": 0}, "BAD_ARGUMENTS"),
    ({"sql": "select 1", "timeout_seconds": 59}, "TIMEOUT_TOO_LARGE"),
    ({"sql": "select 1", "database": "postgres user=postgres"}, "BAD_ARGUMENTS"),
    ({"sql": "select 1", "database": "host=evil"}, "BAD_ARGUMENTS"),
])
def test_bad_arguments_are_refused(tmp_path: Path, arguments: dict, error: str):
    reply = sql_query(runtime(tmp_path), SID, **arguments)
    assert reply.is_error and reply.header["error"] == error


def test_a_bad_session_id_is_refused(tmp_path: Path):
    assert sql_query(runtime(tmp_path), "nope", "select 1").header == {"error": "BAD_SESSION_ID"}


def test_without_the_password_variable_it_is_unavailable_and_leaves_no_call_dir(tmp_path: Path):
    rt = runtime(tmp_path, passwordVariable="MCP_TEST_SQL_ABSENT")
    reply = sql_query(rt, SID, "select 1")
    assert reply.is_error and reply.header["error"] == "SQL_UNAVAILABLE"
    assert "MCP_TEST_SQL_ABSENT" in reply.header["message"]
    assert list((rt.home / "runs" / SID).iterdir()) == []


def test_without_psql_on_the_path_it_is_unavailable(tmp_path: Path):
    reply = sql_query(runtime(tmp_path, psql="no-such-psql-mh-mcp"), SID, "select 1")
    assert reply.is_error and reply.header["error"] == "SQL_UNAVAILABLE"
    assert "no-such-psql-mh-mcp" in reply.header["message"]


# ==================== against PostgreSQL ====================

@needs_pg
def test_a_select_returns_csv_with_null_empty_quotes_and_cyrillic(tmp_path: Path, reader):
    role, _, password = reader
    reply = sql_query(runtime(tmp_path, role, password), SID,
                      "select 1 as a, null::text as n, '' as e, 'б,\"x\"' as q, length('бв') as l")
    assert not reply.is_error
    assert reply.text == 'a,n,e,q,l\n1,<NULL>,,"б,""x""",2'
    assert (reply.header["exitCode"], reply.header["rows"], reply.header["returned"]) == (0, 1, 1)
    assert "truncated" not in reply.header and "stderr" not in reply.header
    assert Path(reply.header["logPath"]).name == "output.csv" and reply.call_id == reply.header["callId"]


@needs_pg
def test_max_rows_cuts_the_reply_while_the_log_keeps_the_whole_result(tmp_path: Path, reader):
    role, _, password = reader
    reply = sql_query(runtime(tmp_path, role, password), SID, "select g from generate_series(1, 10) g", max_rows=3)
    assert reply.text.splitlines() == ["g", "1", "2", "3"]
    assert (reply.header["rows"], reply.header["returned"], reply.header["truncated"]) == (10, 3, True)
    assert Path(reply.header["logPath"]).read_text(encoding="utf-8").split() == ["g", *map(str, range(1, 11))]


@needs_pg
def test_the_default_database_and_the_role_come_from_config_not_from_pg_variables(tmp_path: Path, reader):
    role, _, password = reader
    rt = runtime(tmp_path, role, password)
    save_state(rt.home, SID, SessionState(cwd=tmp_path, env_set={"PGUSER": "postgres", "PGDATABASE": "nope",
                                                                  "PGHOST": "nowhere.invalid"}))
    reply = sql_query(rt, SID, "select current_user as u, current_database() as d")
    assert reply.text.splitlines() == ["u,d", f"{role},{DB}"]


@needs_pg
@pytest.mark.parametrize("command", ["\\! echo PWNED > {target}", "select 1 \\o {target}", "\\unrestrict guess"])
def test_a_backslash_command_is_refused_by_restricted_mode(tmp_path: Path, reader, command: str):
    role, _, password = reader
    target = tmp_path / "written.txt"
    reply = sql_query(runtime(tmp_path, role, password), SID, command.format(target=target.as_posix()))
    assert reply.header["exitCode"] == 3
    assert "restrict" in reply.header["stderr"] or "wrong key" in reply.header["stderr"]
    assert not target.exists()


@needs_pg
def test_an_ordinary_write_fails_as_a_read_only_transaction(tmp_path: Path, reader):
    role, table, password = reader
    reply = sql_query(runtime(tmp_path, role, password), SID, f"insert into public.{table} values (3)")
    assert reply.header["exitCode"] == 3 and "read-only transaction" in reply.header["stderr"]
    assert superuser(f"select count(*) from public.{table}") == "2"


@needs_pg
def test_a_write_with_read_only_lifted_is_still_refused_by_the_role(tmp_path: Path, reader):
    role, table, password = reader
    reply = sql_query(runtime(tmp_path, role, password), SID,
                      f"commit; set default_transaction_read_only = off; insert into public.{table} values (4)")
    assert reply.header["exitCode"] == 3 and "permission denied" in reply.header["stderr"]
    assert superuser(f"select count(*) from public.{table}") == "2"


@needs_pg
def test_an_sql_error_is_exit_3_with_its_text(tmp_path: Path, reader):
    role, _, password = reader
    reply = sql_query(runtime(tmp_path, role, password), SID, "select nope")
    assert reply.header["exitCode"] == 3 and 'column "nope" does not exist' in reply.header["stderr"]
    assert reply.text is None and reply.header["rows"] == 0


@needs_pg
def test_the_statement_timeout_ends_a_long_query_before_the_deadline(tmp_path: Path, reader):
    role, _, password = reader
    reply = sql_query(runtime(tmp_path, role, password), SID, "select pg_sleep(10)", timeout_seconds=4)
    assert reply.header["exitCode"] == 3 and "statement timeout" in reply.header["stderr"]
    assert "timedOut" not in reply.header and reply.header["seconds"] < 4


@needs_pg
def test_a_wrong_password_is_exit_2_with_psql_s_message(tmp_path: Path, reader):
    role, _, _ = reader
    reply = sql_query(runtime(tmp_path, role, "wrong-password"), SID, "select 1")
    assert reply.header["exitCode"] == 2 and "password authentication failed" in reply.header["stderr"]
