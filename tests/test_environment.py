import os

import pytest

from mh_mcp_server.environment import VOLATILE, compose_env, expand, expand_hive, mask, read_registry_env


def compose(machine=None, user=None, server_env=None, fixed=None, config_env=None, path_prepend=(),
            delta_set=None, delta_removed=()):
    return compose_env(machine or {}, user or {}, server_env or {}, fixed or {}, config_env or {}, path_prepend,
                       delta_set or {}, delta_removed)


def test_volatile_is_the_list_of_decision_12():
    assert VOLATILE == ("SYSTEMROOT", "WINDIR", "COMPUTERNAME", "USERNAME", "USERDOMAIN", "USERPROFILE",
                        "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "PROGRAMDATA",
                        "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "PROCESSOR_ARCHITECTURE",
                        "NUMBER_OF_PROCESSORS")


def test_a_later_layer_wins_at_every_step():
    assert compose(machine={"TEMP": "m"})["TEMP"] == "m"
    assert compose(machine={"TEMP": "m"}, user={"TEMP": "u"})["TEMP"] == "u"
    assert compose(user={"TEMP": "u"}, server_env={"TEMP": "s"})["TEMP"] == "s"
    assert compose(server_env={"TEMP": "s"}, fixed={"TEMP": "f"})["TEMP"] == "f"
    assert compose(fixed={"TEMP": "f"}, config_env={"TEMP": "c"})["TEMP"] == "c"
    assert compose(config_env={"TEMP": "c"}, delta_set={"TEMP": "d"})["TEMP"] == "d"


def test_every_layer_contributes_its_own_names():
    env = compose(machine={"M": "1"}, user={"U": "2"}, server_env={"USERNAME": "serg"},
                  fixed={"MH_MCP_SESSION_ID": "20261023-154207-0a1b2c", "MH_MCP_CALL_ID": "0007"},
                  config_env={"PYTHONUTF8": "1"}, delta_set={"MCP_T": "бар"})
    assert env == {"M": "1", "U": "2", "USERNAME": "serg", "MH_MCP_SESSION_ID": "20261023-154207-0a1b2c",
                   "MH_MCP_CALL_ID": "0007", "PYTHONUTF8": "1", "MCP_T": "бар"}


def test_path_is_prepend_then_machine_then_user():
    env = compose(machine={"Path": "C:\\Windows\\system32;C:\\Windows"}, user={"Path": "C:\\u\\bin"},
                  path_prepend=["C:\\p1", "C:\\p2"])
    paths = [v for k, v in env.items() if k.casefold() == "path"]
    assert paths == ["C:\\p1;C:\\p2;C:\\Windows\\system32;C:\\Windows;C:\\u\\bin"]


def test_path_from_one_hive_only_is_kept_as_it_is():
    assert compose(machine={"PATH": "C:\\m"})["PATH"] == "C:\\m"
    assert compose(user={"PATH": "C:\\u"})["PATH"] == "C:\\u"


def test_path_trailing_separators_do_not_leave_empty_entries():
    assert compose(machine={"PATH": "C:\\m;"}, user={"PATH": ";C:\\u;"})["PATH"] == "C:\\m;C:\\u"


def test_path_prepend_without_any_registry_path():
    assert compose(path_prepend=["C:\\p"]) == {"PATH": "C:\\p"}


def test_path_and_Path_collapse_into_one_key_with_the_casing_of_the_last_layer():
    env = compose(machine={"Path": "C:\\m"}, user={"PATH": "C:\\u"})
    assert env == {"PATH": "C:\\m;C:\\u"}
    env = compose(machine={"PATH": "C:\\m"}, user={"Path": "C:\\u"})
    assert env == {"Path": "C:\\m;C:\\u"}


def test_a_removed_name_is_absent_even_when_the_registry_has_it():
    env = compose(machine={"MCP_T": "m"}, user={"MCP_T": "u"}, delta_removed=["mcp_t"])
    assert "MCP_T" not in env and "mcp_t" not in env


def test_a_server_variable_outside_volatile_is_absent():
    env = compose(server_env={"PYTHONPATH": "C:\\x\\src", "USERPROFILE": "C:\\Users\\serg", "Path": "C:\\server"})
    assert env == {"USERPROFILE": "C:\\Users\\serg"}


def test_a_volatile_name_matches_case_insensitively():
    assert compose(server_env={"SystemRoot": "C:\\Windows"}) == {"SystemRoot": "C:\\Windows"}


def test_mask_replaces_every_occurrence():
    assert mask("pw=s3cr3t; again s3cr3t", {"PGPASSWORD": "s3cr3t"}) == "pw=***; again ***"


def test_mask_masks_a_secret_containing_another_secret_whole():
    secrets = {"A": "abc", "B": "abcdef"}
    assert mask("x abcdef y abc z", secrets) == "x *** y *** z"


def test_mask_ignores_empty_values_and_leaves_other_text():
    assert mask("nothing here", {"A": "", "B": "zzz"}) == "nothing here"


def test_expand_replaces_known_names_ignoring_case():
    assert expand("%SystemRoot%\\system32", {"SYSTEMROOT": "C:\\Windows"}) == "C:\\Windows\\system32"


def test_expand_keeps_an_unknown_reference_verbatim():
    assert expand("%NOPE%\\bin", {}) == "%NOPE%\\bin"


def test_expand_continues_after_a_lone_percent():
    assert expand("100% and %X%", {"X": "x"}) == "100% and x"
    assert expand("50%", {"X": "x"}) == "50%"
    assert expand("%%X%", {"X": "x"}) == "%x"


def test_expand_hive_sees_its_own_values_and_the_base():
    hive = expand_hive({"JAVA_HOME": "C:\\jdk"},
                       {"Path": "%JAVA_HOME%\\bin;%TOOLS%", "TOOLS": "%USERPROFILE%\\tools"},
                       {"USERPROFILE": "C:\\Users\\serg"})
    assert hive == {"JAVA_HOME": "C:\\jdk", "Path": "C:\\jdk\\bin;C:\\Users\\serg\\tools",
                    "TOOLS": "C:\\Users\\serg\\tools"}


def test_expand_hive_prefers_the_hive_value_over_a_stale_base_value():
    hive = expand_hive({"JAVA_HOME": "C:\\jdk-25"}, {"Path": "%JAVA_HOME%\\bin"}, {"JAVA_HOME": "C:\\jdk-21"})
    assert hive["Path"] == "C:\\jdk-25\\bin"


@pytest.mark.skipif(os.name != "nt", reason="Windows registry")
def test_the_real_machine_registry_path_contains_the_windows_directory():
    machine = read_registry_env("machine")
    paths = [v for k, v in machine.items() if k.casefold() == "path"]
    assert len(paths) == 1
    assert "c:\\windows" in paths[0].casefold()
    assert "%systemroot%" not in paths[0].casefold()
