from pathlib import Path

import pytest

from mh_mcp_server.config import (Config, ConfigError, DEFAULT_CONFIG_TOML, HOME_VARIABLE, excluded_by, is_under,
                                  load_config, read_text_if_exists, resolve_home)

HOME = Path("C:/_mh/mh-mcp-server-test-home")


def absent(_path: Path) -> None:
    return None


def text_of(text: str):
    return lambda _path: text


def test_defaults_when_the_file_is_absent():
    c = load_config(HOME, absent)
    assert c.shell_executable == "pwsh"
    assert c.shell_args == ("-NoLogo", "-NoProfile", "-NonInteractive", "-File")
    assert c.default_foreground_seconds == 55
    assert c.max_foreground_seconds == 58
    assert c.default_cwd == Path("C:/sandbox/_work")
    assert c.max_lines == 150
    assert c.max_bytes == 24576
    assert c.read_max_lines == 400
    assert c.read_max_bytes == 49152
    assert c.write_roots == (Path("C:/sandbox/_work"),)
    assert c.read_roots == (Path("C:/sandbox/_work"), Path("C:/_mh/mhbp_home-PostgreSQL-disk-storage"))
    assert dict(c.env) == {"PYTHONUTF8": "1"}
    assert c.path_prepend == ()
    assert c.secret_names == frozenset()
    assert c.excluded_paths == ()


def test_the_default_file_text_loads_to_the_defaults():
    assert load_config(HOME, text_of(DEFAULT_CONFIG_TOML)) == load_config(HOME, absent)


@pytest.mark.parametrize("toml, attribute, expected", [
    ('[shell]\nexecutable = "C:/pwsh/pwsh.exe"', "shell_executable", "C:/pwsh/pwsh.exe"),
    ('[shell]\nargs = ["-NoProfile", "-File"]', "shell_args", ("-NoProfile", "-File")),
    ('[execution]\ndefaultForegroundSeconds = 30', "default_foreground_seconds", 30),
    ('[execution]\nmaxForegroundSeconds = 170', "max_foreground_seconds", 170),
    ('[execution]\ndefaultCwd = "C:/sandbox/_work/temp"', "default_cwd", Path("C:/sandbox/_work/temp")),
    ('[output]\nmaxLines = 10', "max_lines", 10),
    ('[output]\nmaxBytes = 1000', "max_bytes", 1000),
    ('[read]\nmaxLines = 20', "read_max_lines", 20),
    ('[read]\nmaxBytes = 2000', "read_max_bytes", 2000),
    ('[paths]\nwriteRoots = ["C:/a", "C:/b"]', "write_roots", (Path("C:/a"), Path("C:/b"))),
    ('[paths]\nreadRoots = ["C:/r"]', "read_roots", (Path("C:/r"),)),
    ('[path]\nprepend = ["C:/tools/bin"]', "path_prepend", ("C:/tools/bin",)),
    ('[secrets]\nnames = ["PGPASSWORD", "NRVV_REST_AUTH"]', "secret_names",
     frozenset({"PGPASSWORD", "NRVV_REST_AUTH"})),
    ('[excluded]\npaths = ["C:\\\\Windows", "C:/sandbox/_work/secret"]', "excluded_paths",
     ("C:\\Windows", "C:/sandbox/_work/secret")),
])
def test_each_key_overrides_its_default(toml: str, attribute: str, expected):
    assert getattr(load_config(HOME, text_of(toml)), attribute) == expected


def test_env_values_are_added_to_the_default_env():
    c = load_config(HOME, text_of('[env]\nPGPASSWORD = "pw"\nPYTHONUTF8 = "0"'))
    assert dict(c.env) == {"PYTHONUTF8": "0", "PGPASSWORD": "pw"}


def test_a_key_left_out_keeps_its_default_beside_an_overridden_one():
    c = load_config(HOME, text_of('[output]\nmaxLines = 10'))
    assert c.max_lines == 10
    assert c.max_bytes == 24576


@pytest.mark.parametrize("toml, named", [
    ('[shell]\nexecutabel = "pwsh"', "shell.executabel"),
    ('[output]\nmaxline = 10', "output.maxline"),
    ('[shel]\nexecutable = "pwsh"', "shel"),
    ('rootKey = 1', "rootKey"),
])
def test_an_unknown_table_or_key_raises_naming_it(toml: str, named: str):
    with pytest.raises(ConfigError) as e:
        load_config(HOME, text_of(toml))
    assert f"'{named}'" in str(e.value)


@pytest.mark.parametrize("toml, named", [
    ('[output]\nmaxLines = "150"', "output.maxLines"),
    ('[output]\nmaxLines = 0', "output.maxLines"),
    ('[output]\nmaxLines = true', "output.maxLines"),
    ('[shell]\nargs = "-File"', "shell.args"),
    ('[paths]\nwriteRoots = ["C:/a", 1]', "paths.writeRoots"),
    ('[env]\nX = 1', "env.X"),
])
def test_a_wrong_type_raises_naming_the_key(toml: str, named: str):
    with pytest.raises(ConfigError) as e:
        load_config(HOME, text_of(toml))
    assert f"'{named}'" in str(e.value)


def test_unparsable_toml_raises():
    with pytest.raises(ConfigError):
        load_config(HOME, text_of('[shell\nexecutable = '))


def test_max_foreground_below_default_foreground_raises():
    with pytest.raises(ConfigError) as e:
        load_config(HOME, text_of('[execution]\ndefaultForegroundSeconds = 30\nmaxForegroundSeconds = 20'))
    assert "maxForegroundSeconds" in str(e.value)


def test_max_foreground_equal_to_default_foreground_is_accepted():
    c = load_config(HOME, text_of('[execution]\ndefaultForegroundSeconds = 40\nmaxForegroundSeconds = 40'))
    assert (c.default_foreground_seconds, c.max_foreground_seconds) == (40, 40)


def test_a_real_config_file_is_read_with_a_bom(tmp_path: Path):
    (tmp_path / "config.toml").write_bytes('\ufeff[output]\nmaxLines = 7\n'.encode("utf-8"))
    assert load_config(tmp_path, read_text_if_exists).max_lines == 7


def test_a_real_home_without_config_gives_the_defaults(tmp_path: Path):
    assert load_config(tmp_path, read_text_if_exists) == load_config(HOME, absent)


def no_registry(_name: str) -> None:
    return None


def test_resolve_home_prefers_the_process_env_over_user_over_machine():
    user = {HOME_VARIABLE: "C:/user-home"}.get
    machine = {HOME_VARIABLE: "C:/machine-home"}.get
    assert resolve_home({HOME_VARIABLE: "C:/process-home"}, user, machine) == Path("C:/process-home")
    assert resolve_home({}, user, machine) == Path("C:/user-home")
    assert resolve_home({}, no_registry, machine) == Path("C:/machine-home")


def test_resolve_home_is_none_when_all_three_are_absent():
    assert resolve_home({"OTHER": "x"}, no_registry, no_registry) is None


def test_resolve_home_ignores_the_case_of_the_process_env_name():
    assert resolve_home({"mh_mcp_server_home": "C:/h"}, no_registry, no_registry) == Path("C:/h")


def test_resolve_home_treats_an_empty_value_as_absent():
    assert resolve_home({HOME_VARIABLE: ""}, {HOME_VARIABLE: "C:/u"}.get, no_registry) == Path("C:/u")


ROOTS = (Path("C:/sandbox/_work"),)


def test_is_under_accepts_a_child_and_the_root_itself():
    assert is_under(Path("C:/sandbox/_work/temp/x.txt"), ROOTS)
    assert is_under(Path("C:/sandbox/_work"), ROOTS)


def test_is_under_refuses_a_sibling_sharing_a_prefix():
    assert not is_under(Path("C:/sandbox/_work2"), ROOTS)
    assert not is_under(Path("C:/sandbox/_work2/a"), ROOTS)


def test_is_under_refuses_a_dot_dot_escape():
    assert not is_under(Path("C:/sandbox/_work/../x"), ROOTS)


def test_is_under_ignores_case():
    assert is_under(Path("c:/SANDBOX/_Work/Temp"), ROOTS)
    assert is_under(Path("C:/sandbox/_work/temp"), (Path("C:/Sandbox/_WORK"),))


def test_is_under_checks_every_root():
    roots = (Path("C:/sandbox/_work"), Path("C:/_mh/mhbp_home-PostgreSQL-disk-storage"))
    assert is_under(Path("C:/_mh/mhbp_home-PostgreSQL-disk-storage/logs/a.log"), roots)
    assert not is_under(Path("C:/_mh/other"), roots)


EXCLUDED = ("C:\\Windows", "c:/sandbox/_work/Secret")


def test_excluded_by_matches_the_start_of_a_path_ignoring_case_and_separators():
    assert excluded_by(Path("C:/Windows/win.ini"), EXCLUDED) == "C:\\Windows"
    assert excluded_by(Path("c:\\windows"), EXCLUDED) == "C:\\Windows"
    assert excluded_by(Path("C:/sandbox/_work/secret/a.txt"), EXCLUDED) == "c:/sandbox/_work/Secret"


def test_excluded_by_is_a_plain_string_prefix():
    # C:\Windows covers C:\WindowsApps too; an item ending in a separator covers one directory only
    assert excluded_by(Path("C:/WindowsApps/x"), EXCLUDED) == "C:\\Windows"
    assert excluded_by(Path("C:/WindowsApps/x"), ("C:\\Windows\\",)) is None
    assert excluded_by(Path("C:/Windows/x"), ("C:\\Windows\\",)) == "C:\\Windows\\"


def test_excluded_by_is_none_outside_every_item_and_for_no_items():
    assert excluded_by(Path("C:/sandbox/_work/temp/a.txt"), EXCLUDED) is None
    assert excluded_by(Path("C:/Windows"), ()) is None
    assert excluded_by(Path("C:/Windows"), ("",)) is None


def test_config_is_immutable():
    c = load_config(HOME, absent)
    with pytest.raises(AttributeError):
        c.max_lines = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        c.env["X"] = "y"  # type: ignore[index]
    assert isinstance(c, Config)
