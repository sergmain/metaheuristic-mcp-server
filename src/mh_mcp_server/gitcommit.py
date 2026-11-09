"""gitCommit (0.1.12): commit exactly the named paths, then push - git run directly, no pwsh.

What it is for is the rule, not speed (a push is network time). Chats share one working tree and one index per repo, and
a commit without a pathspec takes whatever is staged, another chat's files included. mh-mcp's transcripts of
2026-10-27 - 11-09 (temp/mh-mcp-usage-2.py): 267 of 345 commits named no path, and 17 commands staged with `git add -A`
or `git add .`.

The steps, each a `git` process with argv; the message goes on stdin, so nothing is quoted anywhere:
  git --literal-pathspecs add -- <paths>                        new, modified and deleted files alike
  git --literal-pathspecs commit -q -F - -- <paths>             only these paths: another path's staged change stays out,
                                                                and stays staged
  git push origin HEAD                                          unless push is false
Probed on git 2.45 (2026-11-09): a deleted path commits as D after the add; an untracked path needs the add first; a
message with Cyrillic, quotes, $ and backticks arrives byte for byte.

A path names a file, or a file deleted since HEAD, under the repository's top level, and naming what goes into the commit
is the point. So the top level itself, an existing directory and a wildcard are refused, and every path obeys writeFile's
roots and [excluded]. A step still running at the call's deadline is killed (children.run_child) and reported as
`timedOut`.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Sequence

from mh_mcp_server.children import Finished, env_value, run_child, without
from mh_mcp_server.config import excluded_by, is_under
from mh_mcp_server.execution import Reply, Runtime, command_env, refusal
from mh_mcp_server.files import _target
from mh_mcp_server.session_id import is_valid_session_id
from mh_mcp_server.session_state import load_state

PATHS_MAX = 200
WILDCARDS = frozenset("*?[")
OUTPUT_CHARS = 8000
# variables that would point git at another repository, index or work tree than the one named
REPO_VARIABLES = frozenset(name.casefold() for name in (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM"))


# ==================== pure ====================

def git_env(env: dict[str, str]) -> dict[str, str]:
    """The command environment without the repository-locating GIT_* variables; git never prompts for credentials."""
    return {**without(env, lambda name: name.casefold() in REPO_VARIABLES), "GIT_TERMINAL_PROMPT": "0"}


def relative_parts(path: Path, top: Path) -> str:
    """`path` (already known to lie under `top`, by is_under) relative to `top`, with / - by components, since the two
    may differ in the case of a drive letter or a directory."""
    return Path(*path.parts[len(top.parts):]).as_posix()


def message_bytes(message: str) -> bytes:
    return message.replace("\r\n", "\n").encode("utf-8")


def text_of(*outputs: bytes) -> str:
    text = "\n".join(o.decode("utf-8", "replace").replace("\r\n", "\n").strip() for o in outputs if o.strip())
    return text if len(text) <= OUTPUT_CHARS else text[:OUTPUT_CHARS] + "\n..."


# ==================== the tool ====================

def git_commit(rt: Runtime, session_id: str, repo: str, paths: Sequence[str], message: str, push: bool = True,
               timeout_seconds: int | None = None) -> Reply:
    if not is_valid_session_id(session_id):
        return refusal("BAD_SESSION_ID")
    timeout = rt.config.default_foreground_seconds if timeout_seconds is None else timeout_seconds
    if timeout > rt.config.max_foreground_seconds:
        return refusal("TIMEOUT_TOO_LARGE", session_id, max=rt.config.max_foreground_seconds)
    if not paths or len(paths) > PATHS_MAX or not message.strip() or timeout < 1:
        return refusal("BAD_ARGUMENTS", session_id,
                       message=f"paths: 1 - {PATHS_MAX}, message must not be blank, timeoutSeconds >= 1")
    target = _target(rt, session_id, repo, rt.config.write_roots)
    if isinstance(target, Reply):
        return target
    if not target.is_dir():
        return refusal("NOT_FOUND", session_id, path=str(target))
    started = time.monotonic()
    deadline = started + timeout
    env = git_env(command_env(rt, session_id, "gitCommit", load_state(rt.home, session_id, rt.config.default_cwd)))
    git = shutil.which("git", path=env_value(env, "PATH"))
    if git is None:
        return refusal("GIT_UNAVAILABLE", session_id, message="git not found on the command PATH")

    def run(*args: str, stdin: bytes = b"", cwd: Path = target) -> Finished:
        return run_child([git, *args], env, cwd, stdin, deadline - time.monotonic())

    found = run("rev-parse", "--show-toplevel")
    if found.exit_code != 0:
        return refusal("NOT_A_REPO", session_id, path=str(target), message=text_of(found.stderr))
    top = Path(found.stdout.decode("utf-8").strip()).resolve()
    relatives: list[str] = []
    for path in paths:
        if not path.strip() or WILDCARDS & set(path):
            return refusal("BAD_ARGUMENTS", session_id, path=path,
                           message="a path names one file: no wildcard (*, ?, [), not blank")
        full = (target / path).resolve()
        excluded = excluded_by(full, rt.config.excluded_paths)
        if excluded is not None:
            return refusal("PATH_EXCLUDED", session_id, path=str(full), excluded=excluded)
        if not is_under(full, rt.config.write_roots):
            return refusal("PATH_OUTSIDE_ROOTS", session_id, path=str(full))
        if not is_under(full, [top]):
            return refusal("PATH_OUTSIDE_REPO", session_id, path=str(full), repo=str(top))
        if len(full.parts) == len(top.parts) or full.is_dir():
            return refusal("BAD_ARGUMENTS", session_id, path=str(full),
                           message="a path names one file - not the repository, not a directory")
        relative = relative_parts(full, top)
        if relative not in relatives:
            relatives.append(relative)

    header: dict[str, Any] = {"repo": str(top)}

    def finish(step: str | None, finished: Finished | None, *outputs: bytes) -> Reply:
        if step is not None:
            header["step"] = step
            header["exitCode"] = finished.exit_code
            if finished.timed_out:
                header["timedOut"] = True
        else:
            header["exitCode"] = 0
        header["seconds"] = round(time.monotonic() - started, 2)
        return Reply(header, text_of(*outputs) or None, False, None, session_id)

    added = run("--literal-pathspecs", "add", "--", *relatives, cwd=top)
    if added.exit_code != 0:
        return finish("add", added, added.stdout, added.stderr)
    committed = run("-c", "core.quotepath=false", "--literal-pathspecs", "commit", "-q", "-F", "-", "--", *relatives,
                    stdin=message_bytes(message), cwd=top)
    if committed.exit_code != 0:
        return finish("commit", committed, committed.stdout, committed.stderr)
    shown = run("-c", "core.quotepath=false", "show", "--name-status", "--format=%h %s", "HEAD", cwd=top)
    branch = run("rev-parse", "--abbrev-ref", "HEAD", cwd=top)
    header.update({"branch": branch.stdout.decode("utf-8", "replace").strip(),
                   "commit": shown.stdout.decode("utf-8", "replace").split(" ", 1)[0].strip(),
                   "files": len(relatives)})
    if not push:
        header["pushed"] = False
        return finish(None, None, shown.stdout)
    pushed = run("push", "origin", "HEAD", cwd=top)
    header["pushed"] = pushed.exit_code == 0
    if pushed.exit_code != 0:
        return finish("push", pushed, shown.stdout, pushed.stdout, pushed.stderr)
    return finish(None, None, shown.stdout, pushed.stdout, pushed.stderr)
