"""Entry point: `python -m mh_mcp_server [--version]`."""

import sys

from mh_mcp_server import __version__


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--version"]:
        print(__version__)
        return 0
    print("server not built yet", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
