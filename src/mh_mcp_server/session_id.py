"""Session ids: `yyyyMMdd-<6 digits>-<6 lowercase hex>`, the form of run_test.py's run ids (plan 051, decision 4)."""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from typing import Callable

SESSION_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


def new_session_id(now: Callable[[], datetime], hex6: Callable[[], str]) -> str:
    """A fresh id: local date and time of `now()`, then `hex6()` - 6 lowercase hex digits."""
    return f"{now().strftime('%Y%m%d-%H%M%S')}-{hex6()}"


def random_hex6() -> str:
    """The production `hex6` of `new_session_id`."""
    return secrets.token_hex(3)


def is_valid_session_id(value: object) -> bool:
    """The regex (matched whole - a trailing newline is refused) and a first segment that is a real calendar date."""
    if not isinstance(value, str) or SESSION_ID_RE.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value[:8], "%Y%m%d")
    except ValueError:
        return False
    return True
