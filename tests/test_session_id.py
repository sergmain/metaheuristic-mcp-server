from datetime import datetime

import pytest

from mh_mcp_server.session_id import SESSION_ID_RE, is_valid_session_id, new_session_id, random_hex6


def test_a_minted_id_has_the_format_and_is_valid():
    sid = new_session_id(lambda: datetime(2026, 10, 23, 15, 42, 7), lambda: "0a1b2c")
    assert sid == "20261023-154207-0a1b2c"
    assert is_valid_session_id(sid)


def test_a_minted_id_with_the_production_hex_is_valid():
    sid = new_session_id(datetime.now, random_hex6)
    assert SESSION_ID_RE.fullmatch(sid) is not None
    assert is_valid_session_id(sid)


def test_random_hex6_is_six_lowercase_hex_digits():
    for _ in range(50):
        value = random_hex6()
        assert len(value) == 6 and value == value.lower() and int(value, 16) >= 0


def test_a_run_test_py_run_id_is_a_valid_session_id():
    assert is_valid_session_id("20261022-105608-207a46")


@pytest.mark.parametrize("value", [
    "20261322-105608-207a46",   # month 13
    "20260230-105608-207a46",   # 30 February
    "20261022-105608-207A46",   # uppercase hex
    "20261022-1056-207a46",     # short second segment
    "20261022-105608-207a4",    # short hex
    "20261022-105608-207a46\n", # trailing newline
    " 20261022-105608-207a46",
    "20261022_105608_207a46",
    "",
    None,
    20261022105608,
])
def test_invalid_ids_are_refused(value):
    assert not is_valid_session_id(value)
