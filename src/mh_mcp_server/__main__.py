"""Entry point: `python -m mh_mcp_server` runs the stdio MCP server; `--version` prints the version.

stdout is the JSON-RPC channel (decision 15): nothing but the SDK writes to it. Diagnostics go to stderr and to
`%MH_MCP_SERVER_HOME%/logs/server.log`.

Since 0.1.7 the log file is per instance - `logs/server-<sid>.log`, `<sid>` minted like a sessionId - so the instances
Claude Desktop starts side by side never share (or rotate) one file; at start the newest `LOG_FILES_KEPT` are kept.
"""

import logging
import logging.handlers
import os
import sys
from datetime import datetime
from pathlib import Path

from mh_mcp_server import __version__

LOG_FILES_KEPT = 20


def _registry_value(hive: str):
    def read(name: str) -> str | None:
        from mh_mcp_server.environment import read_registry_env
        try:
            values = read_registry_env(hive)  # type: ignore[arg-type]
        except OSError:
            return None
        for key, value in values.items():
            if key.casefold() == name.casefold():
                return value
        return None
    return read


def instance_log_name(instance_id: str) -> str:
    """The log file of one server instance; `instance_id` is minted like a sessionId (`yyyyMMdd-HHmmss-<6 hex>`), so
    no two instances share a file and name order is start order."""
    return f"server-{instance_id}.log"


def prune_logs(logs: Path, keep: int) -> None:
    """Removes all but the newest `keep` instance logs; their names start with the start time, so name order is age
    order. A file another instance still holds open cannot be deleted on Windows and is skipped."""
    names = sorted(logs.glob("server-*.log"))
    for old in names[:max(len(names) - keep, 0)]:
        try:
            old.unlink()
        except OSError:
            pass


def setup_logging(home: Path) -> None:
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    prune_logs(logs, LOG_FILES_KEPT - 1)   # room for this instance's own file
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s [pid %(process)d] %(message)s")
    from mh_mcp_server.session_id import new_session_id, random_hex6
    file_handler = logging.FileHandler(logs / instance_log_name(new_session_id(datetime.now, random_hex6)),
                                       encoding="utf-8")
    file_handler.setFormatter(formatter)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)
    logging.getLogger("mh_mcp_server").setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--version"]:
        print(__version__)
        return 0
    if args:
        print("usage: python -m mh_mcp_server [--version]", file=sys.stderr)
        return 2

    from mh_mcp_server.config import HOME_VARIABLE, ConfigError, load_config, read_text_if_exists, resolve_home
    home = resolve_home(os.environ, _registry_value("user"), _registry_value("machine"))
    if home is None:
        print(f"mh-mcp-server: {HOME_VARIABLE} is not set (process environment, HKCU\\Environment, machine)",
              file=sys.stderr)
        return 2
    try:
        config = load_config(home, read_text_if_exists)
    except ConfigError as e:
        print(f"mh-mcp-server: {e}", file=sys.stderr)
        return 2
    setup_logging(home)

    from mh_mcp_server.execution import production_runtime
    from mh_mcp_server.server import build_server, serve_stdio
    logging.getLogger("mh_mcp_server").info("starting %s, home %s", __version__, home)
    import anyio
    anyio.run(serve_stdio, build_server(production_runtime(home, config)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
