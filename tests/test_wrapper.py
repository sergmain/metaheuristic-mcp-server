import os
import shutil
from pathlib import Path

import pytest

from mh_mcp_server.session_state import SessionState, apply_state_out
from mh_mcp_server.wrapper import ps_literal, render_wrapper
from support import run_wrapped

NOT_PERSISTED = frozenset({"MH_MCP_SESSION_ID", "MH_MCP_CALL_ID"})


def env_of(state: SessionState) -> dict[str, str]:
    """A test command environment: this process's environment, PYTHONUTF8 as config [env] sets it, the delta."""
    removed = {n.casefold() for n in state.env_removed}
    env = {k: v for k, v in os.environ.items() if k.casefold() not in removed}
    env["PYTHONUTF8"] = "1"
    env.update(state.env_set)
    return env


def session_call(home: Path, state: SessionState, command: str) -> tuple[SessionState, dict, str]:
    record, out, state_out, _ = run_wrapped(home, command, state.cwd, env_of(state))
    assert state_out is not None, out
    new_state, _ = apply_state_out(state, state_out, (home,), NOT_PERSISTED)
    return new_state, record, out


@pytest.mark.parametrize("command, expected", [
    ("exit 3", 3),
    ("cmd /c exit 7", 7),
    ("Get-Item C:/sandbox/_work/temp/__absent__", 1),
    ("cmd /c exit 7; 'after'", 0),
    ("throw 'x'", 1),
    ("'a' && cmd /c exit 5 && 'b'", 5),
])
def test_the_exit_code_rule_and_state_out_in_every_case(tmp_path: Path, command: str, expected: int):
    record, out, state_out, _ = run_wrapped(tmp_path, command, tmp_path)
    assert record["exit"] == expected, out
    assert state_out is not None, out
    assert Path(state_out["cwd"]) == tmp_path


def test_a_chain_stops_at_the_failing_native_command(tmp_path: Path):
    _, out, _, _ = run_wrapped(tmp_path, "'a' && cmd /c exit 5 && 'b'", tmp_path)
    assert out.splitlines() == ["a"]


def test_a_terminating_error_is_written_to_the_output(tmp_path: Path):
    _, out, _, _ = run_wrapped(tmp_path, "throw 'boom-x'", tmp_path)
    assert "boom-x" in out


def test_set_location_persists_into_the_next_call(tmp_path: Path):
    (tmp_path / "temp").mkdir()
    state = SessionState(cwd=tmp_path)
    state, record, _ = session_call(tmp_path, state, "Set-Location temp")
    assert record["exit"] == 0
    assert state.cwd == tmp_path / "temp"
    _, _, out = session_call(tmp_path, state, "$PWD.ProviderPath")
    assert out.strip() == str(tmp_path / "temp")


def test_an_env_variable_persists_with_cyrillic_and_its_removal_too(tmp_path: Path):
    state = SessionState(cwd=tmp_path)
    state, _, _ = session_call(tmp_path, state, "$env:MCP_T = 'бар'")
    assert dict(state.env_set) == {"MCP_T": "бар"}
    state, _, out = session_call(tmp_path, state, "\"[$env:MCP_T]\"")
    assert out.strip() == "[бар]"
    state, _, _ = session_call(tmp_path, state, "Remove-Item Env:MCP_T")
    assert dict(state.env_set) == {}
    assert state.env_removed == frozenset({"MCP_T"})
    _, _, out = session_call(tmp_path, state, "\"[$env:MCP_T]\"")
    assert out.strip() == "[]"


def test_state_out_reports_only_the_difference(tmp_path: Path):
    _, _, state_out, _ = run_wrapped(tmp_path, "$env:MCP_A = '1'", tmp_path)
    assert state_out["envSet"] == {"MCP_A": "1"}
    assert state_out["envRemoved"] == []


def test_cyrillic_from_pwsh_and_from_python_comes_back_intact(tmp_path: Path):
    _, out, _, _ = run_wrapped(tmp_path, "\"Привет\"\npython -c \"print('Привет')\"",
                               tmp_path, env_of(SessionState(cwd=tmp_path)))
    assert out.splitlines() == ["Привет", "Привет"]


def test_a_here_string_inside_the_command_works(tmp_path: Path):
    command = "$t = @'\nline 'one'\nline two\n'@\n$t"
    record, out, _, _ = run_wrapped(tmp_path, command, tmp_path)
    assert record["exit"] == 0
    assert out.splitlines() == ["line 'one'", "line two"]


@pytest.mark.skipif(shutil.which("java") is None, reason="no java on PATH")
def test_characterization_a_java_program_printing_cyrillic_into_output_log(tmp_path: Path):
    # Characterization (plan 051 Phase 6): pins today's bytes. With stdout redirected to a file Java 25 takes
    # stdout.encoding from the ANSI code page (Cp1252 on this box), so the Cyrillic text is already lost as '?'
    # before it reaches output.log. Observed 2026-10-23 with Temurin 25.0.2.
    (tmp_path / "Hello.java").write_text(
        'public class Hello { public static void main(String[] a) { System.out.println("Привет"); '
        'System.out.println(System.getProperty("stdout.encoding")); } }\n', encoding="utf-8")
    record, _, _, call_dir = run_wrapped(tmp_path, "java Hello.java", tmp_path)
    assert record["exit"] == 0
    assert (call_dir / "output.log").read_bytes() == b"??????\r\nCp1252\r\n"


def test_ps_literal_doubles_single_quotes():
    assert ps_literal("C:/a'b/state-out.json") == "'C:/a''b/state-out.json'"


def test_the_user_command_starts_on_its_own_line():
    script = render_wrapper("'x'", Path("C:/s/state-out.json"))
    lines = script.splitlines()
    assert lines[3] == "try {"
    assert lines[4] == "'x'"
    assert lines[5] == "$__mcpOk = $?"
    assert "'C:\\s\\state-out.json'" in script or "'C:/s/state-out.json'" in script
