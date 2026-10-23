import os
import subprocess
import sys
import tomllib
from pathlib import Path

from mh_mcp_server import __version__

REPO = Path(__file__).resolve().parent.parent


def test_version_is_printed_by_the_module_and_matches_pyproject():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    result = subprocess.run([sys.executable, "-m", "mh_mcp_server", "--version"], env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode("utf-8").strip() == __version__
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__
