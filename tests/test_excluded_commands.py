"""[excluded] on the command path: executeCommand and startJob must refuse a command that names an excluded path, and
a command that would run in an excluded directory - the rule the file tools already keep (test_files.py)."""

from pathlib import Path

import anyio
import pytest

from mh_mcp_server.config import load_config, read_text_if_exists
from mh_mcp_server.execution import execute_command, get_session, production_runtime
from mh_mcp_server.jobs import start_job
from support import write_home

SID = "20261023-190000-0000ee"

# how a command may spell the same path
SPELLINGS = {
    "as-windows-reports-it": lambda p: str(p),
    "forward-slashes": lambda p: p.as_posix(),
    "upper-case": lambda p: str(p).upper(),
    "doubled-backslashes": lambda p: str(p).replace("\\", "\\\\"),
}


def runtime(tmp_path: Path, extra_toml: str = ""):
    home = write_home(tmp_path, extra_toml)
    return production_runtime(home, load_config(home, read_text_if_exists))


def excluded_secret(tmp_path: Path):
    """`Secret/a.txt` under tmp_path, and a runtime whose [excluded] names `Secret` in another case and with other
    separators than Windows reports it."""
    secret = tmp_path / "Secret"
    secret.mkdir()
    (secret / "a.txt").write_bytes(b"keep\n")
    item = str(secret).replace("\\", "/").lower()
    return secret, item, runtime(tmp_path, f'[excluded]\npaths = ["{item}"]\n')


@pytest.mark.parametrize("spelling", SPELLINGS.values(), ids=SPELLINGS.keys())
def test_execute_command_naming_an_excluded_file_is_refused(tmp_path: Path, spelling):
    secret, item, rt = excluded_secret(tmp_path)
    reply = anyio.run(execute_command, rt, SID, f"Get-Content '{spelling(secret / 'a.txt')}'")
    assert reply.is_error and reply.header == {"error": "PATH_EXCLUDED", "where": "command", "excluded": item}


def test_execute_command_naming_an_excluded_file_reads_nothing(tmp_path: Path):
    secret, item, rt = excluded_secret(tmp_path)
    reply = anyio.run(execute_command, rt, SID, f"Get-Content '{secret / 'a.txt'}'")
    assert reply.is_error and reply.text is None and "exitCode" not in reply.header


def test_start_job_naming_an_excluded_file_is_refused(tmp_path: Path):
    secret, item, rt = excluded_secret(tmp_path)
    reply = start_job(rt, SID, f"Get-Content '{secret / 'a.txt'}'")
    assert reply.is_error and reply.header == {"error": "PATH_EXCLUDED", "where": "command", "excluded": item}
    assert get_session(rt, SID).header["jobs"] == []


def test_execute_command_with_an_excluded_cwd_is_refused(tmp_path: Path):
    secret, item, rt = excluded_secret(tmp_path)
    reply = anyio.run(execute_command, rt, SID, "(Get-Location).Path", str(secret))
    assert reply.is_error and reply.header == {"error": "PATH_EXCLUDED", "where": "cwd", "cwd": str(secret),
                                               "excluded": item}


def test_the_next_command_of_a_session_moved_into_an_excluded_directory_is_refused(tmp_path: Path):
    secret, item, rt = excluded_secret(tmp_path)
    # a relative name: the text of this command names no excluded path
    moved = anyio.run(execute_command, rt, SID, "Set-Location Secret")
    assert not moved.is_error and moved.header["cwd"] == str(secret)
    reply = anyio.run(execute_command, rt, SID, "Get-Content a.txt")
    assert reply.is_error and reply.header == {"error": "PATH_EXCLUDED", "where": "cwd", "cwd": str(secret),
                                               "excluded": item}


def test_a_command_outside_every_excluded_path_runs(tmp_path: Path):
    secret, item, rt = excluded_secret(tmp_path)
    (tmp_path / "open.txt").write_bytes(b"o\n")
    reply = anyio.run(execute_command, rt, SID, f"Get-Content '{tmp_path / 'open.txt'}'")
    assert not reply.is_error and reply.header["exitCode"] == 0 and reply.text == "o"
