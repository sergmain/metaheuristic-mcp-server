"""gitCommit (0.1.12) on real git repositories the tests build in tmp_path (inside the write roots): a bare `origin` and a
work tree `work` on branch main - no mock of git."""

import shutil
import subprocess
from pathlib import Path

import anyio
import pytest

from mh_mcp_server.config import load_config, read_text_if_exists
from mh_mcp_server.execution import production_runtime
from mh_mcp_server.gitcommit import git_commit
from mh_mcp_server.session_state import SessionState, save_state
from support import call, connect, write_home

SID = "20261109-130000-00020b"
GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="no git")


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run([GIT, *args], cwd=cwd, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout.decode("utf-8").strip()


def repos(tmp_path: Path) -> tuple[Path, Path]:
    """origin.git (bare) and work (main, pushed): a.txt, b.txt, gone.txt and sub/s.txt committed."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    for key, value in (("user.name", "mh-mcp test"), ("user.email", "test@example.com"), ("core.autocrlf", "false")):
        git(work, "config", key, value)
    git(work, "remote", "add", "origin", str(origin))
    (work / "sub").mkdir()
    for name in ("a.txt", "b.txt", "gone.txt", "sub/s.txt"):
        (work / name).write_text(name + "\n", encoding="utf-8", newline="\n")
    git(work, "add", "--", "a.txt", "b.txt", "gone.txt", "sub/s.txt")
    git(work, "commit", "-q", "-m", "seed")
    git(work, "push", "-q", "origin", "main")
    return origin, work


def runtime(tmp_path: Path):
    home = write_home(tmp_path)
    return production_runtime(home, load_config(home, read_text_if_exists))


def committed(work: Path) -> list[str]:
    return git(work, "-c", "core.quotepath=false", "show", "--name-status", "--format=", "HEAD").splitlines()


# ==================== what gets committed ====================

def test_only_the_named_paths_are_committed_and_another_staged_change_stays_staged(tmp_path: Path):
    _, work = repos(tmp_path)
    (work / "a.txt").write_text("a2\n", encoding="utf-8")
    (work / "b.txt").write_text("b2\n", encoding="utf-8")
    git(work, "add", "--", "b.txt")            # staged by another chat

    reply = git_commit(runtime(tmp_path), SID, str(work), ["a.txt"], "only a", push=False)

    assert not reply.is_error and reply.header["exitCode"] == 0, reply
    assert (reply.header["files"], reply.header["pushed"], reply.header["branch"]) == (1, False, "main")
    assert committed(work) == ["M\ta.txt"]
    assert git(work, "diff", "--cached", "--name-only") == "b.txt"
    assert reply.header["commit"] == git(work, "rev-parse", "--short", "HEAD")


def test_a_new_file_and_a_deleted_file_are_committed(tmp_path: Path):
    _, work = repos(tmp_path)
    (work / "new.txt").write_text("new\n", encoding="utf-8")
    (work / "gone.txt").unlink()

    reply = git_commit(runtime(tmp_path), SID, str(work), ["new.txt", "gone.txt"], "add and delete", push=False)

    assert reply.header["exitCode"] == 0, reply
    assert committed(work) == ["D\tgone.txt", "A\tnew.txt"]
    assert "D\tgone.txt" in reply.text and "A\tnew.txt" in reply.text


def test_the_message_arrives_byte_for_byte_and_crlf_becomes_lf(tmp_path: Path):
    _, work = repos(tmp_path)
    (work / "a.txt").write_text("a3\n", encoding="utf-8")
    message = 'Коммит "quoted" $x `t` & | ;\r\n\r\nsecond line'

    reply = git_commit(runtime(tmp_path), SID, str(work), ["a.txt"], message, push=False)

    assert reply.header["exitCode"] == 0, reply
    log = subprocess.run([GIT, "log", "-1", "--format=%B"], cwd=work, capture_output=True, timeout=60).stdout
    assert log.decode("utf-8").strip() == message.replace("\r\n", "\n")


def test_a_repo_given_as_a_subdirectory_takes_paths_relative_to_it(tmp_path: Path):
    _, work = repos(tmp_path)
    (work / "sub" / "s.txt").write_text("s2\n", encoding="utf-8")
    (work / "a.txt").write_text("a4\n", encoding="utf-8")

    reply = git_commit(runtime(tmp_path), SID, str(work / "sub"), ["s.txt", "../a.txt", "./s.txt"], "from sub",
                       push=False)

    assert reply.header["exitCode"] == 0, reply
    assert reply.header["files"] == 2 and Path(reply.header["repo"]) == work.resolve()
    assert committed(work) == ["M\ta.txt", "M\tsub/s.txt"]


def test_git_variables_of_the_session_do_not_redirect_the_commit(tmp_path: Path):
    _, work = repos(tmp_path)
    other = tmp_path / "other"
    git(tmp_path, "init", "-q", "-b", "main", str(other))
    rt = runtime(tmp_path)
    save_state(rt.home, SID, SessionState(cwd=tmp_path, env_set={"GIT_DIR": str(other / ".git"),
                                                                  "GIT_INDEX_FILE": str(other / "index")}))
    (work / "a.txt").write_text("a5\n", encoding="utf-8")

    reply = git_commit(rt, SID, str(work), ["a.txt"], "here", push=False)

    assert reply.header["exitCode"] == 0, reply
    assert git(work, "log", "-1", "--format=%s") == "here"


# ==================== push ====================

def test_push_sends_the_commit_to_origin(tmp_path: Path):
    origin, work = repos(tmp_path)
    (work / "a.txt").write_text("a6\n", encoding="utf-8")

    reply = git_commit(runtime(tmp_path), SID, str(work), ["a.txt"], "pushed")

    assert (reply.header["exitCode"], reply.header["pushed"]) == (0, True), reply
    assert git(origin, "rev-parse", "main") == git(work, "rev-parse", "HEAD")


def test_a_failed_push_is_the_push_step_and_the_commit_stays(tmp_path: Path):
    _, work = repos(tmp_path)
    git(work, "remote", "remove", "origin")
    (work / "a.txt").write_text("a7\n", encoding="utf-8")

    reply = git_commit(runtime(tmp_path), SID, str(work), ["a.txt"], "not pushed")

    assert (reply.header["step"], reply.header["pushed"]) == ("push", False)
    assert reply.header["exitCode"] not in (0, None) and "origin" in reply.text
    assert reply.header["commit"] == git(work, "rev-parse", "--short", "HEAD")
    assert git(work, "log", "-1", "--format=%s") == "not pushed"


# ==================== failures and refusals ====================

def test_nothing_to_commit_is_the_commit_step_failing(tmp_path: Path):
    _, work = repos(tmp_path)

    reply = git_commit(runtime(tmp_path), SID, str(work), ["a.txt"], "nothing", push=False)

    assert not reply.is_error and (reply.header["step"], reply.header["exitCode"]) == ("commit", 1)
    assert git(work, "log", "-1", "--format=%s") == "seed"


def test_an_unknown_path_is_the_add_step_failing(tmp_path: Path):
    _, work = repos(tmp_path)

    reply = git_commit(runtime(tmp_path), SID, str(work), ["no-such.txt"], "unknown", push=False)

    assert reply.header["step"] == "add" and reply.header["exitCode"] != 0
    assert "did not match" in reply.text


@pytest.mark.parametrize("paths, message, error", [
    ([], "m", "BAD_ARGUMENTS"),
    (["a.txt"], "  ", "BAD_ARGUMENTS"),
    (["."], "m", "BAD_ARGUMENTS"),
    (["sub"], "m", "BAD_ARGUMENTS"),
    (["*.txt"], "m", "BAD_ARGUMENTS"),
    (["sub/[s].txt"], "m", "BAD_ARGUMENTS"),
    ([""], "m", "BAD_ARGUMENTS"),
    (["../outside.txt"], "m", "PATH_OUTSIDE_REPO"),
    (["C:/Windows/win.ini"], "m", "PATH_OUTSIDE_ROOTS"),
])
def test_refused_paths_and_messages_commit_nothing(tmp_path: Path, paths: list, message: str, error: str):
    _, work = repos(tmp_path)
    (work / "a.txt").write_text("a8\n", encoding="utf-8")
    head = git(work, "rev-parse", "HEAD")

    reply = git_commit(runtime(tmp_path), SID, str(work), paths, message, push=False)

    assert reply.is_error and reply.header["error"] == error, reply
    assert git(work, "rev-parse", "HEAD") == head and git(work, "diff", "--cached", "--name-only") == ""


@pytest.mark.parametrize("repo, error", [
    ("plain", "NOT_A_REPO"),
    ("absent", "NOT_FOUND"),
    ("C:/Users", "PATH_OUTSIDE_ROOTS"),
])
def test_a_repo_that_is_not_one_is_refused(tmp_path: Path, repo: str, error: str):
    (tmp_path / "plain").mkdir()
    reply = git_commit(runtime(tmp_path), SID, repo, ["a.txt"], "m", push=False)
    assert reply.is_error and reply.header["error"] == error, reply


def test_a_bad_session_and_a_too_long_timeout_are_refused(tmp_path: Path):
    rt = runtime(tmp_path)
    assert git_commit(rt, "nope", str(tmp_path), ["a.txt"], "m").header == {"error": "BAD_SESSION_ID"}
    assert git_commit(rt, SID, str(tmp_path), ["a.txt"], "m", timeout_seconds=59).header["error"] == "TIMEOUT_TOO_LARGE"


# ==================== through stdio ====================

def test_git_commit_through_stdio_takes_paths_as_a_list(tmp_path: Path):
    _, work = repos(tmp_path)
    (work / "a.txt").write_text("a9\n", encoding="utf-8")

    async def main():
        async with connect(write_home(tmp_path)) as session:
            return await call(session, "gitCommit", sessionId=SID, repo=str(work), paths=["a.txt"], message="stdio",
                              push=False)

    header, text, is_error = anyio.run(main)
    assert not is_error and header["exitCode"] == 0, header
    assert text.splitlines()[0].endswith(" stdio") and "M\ta.txt" in text
