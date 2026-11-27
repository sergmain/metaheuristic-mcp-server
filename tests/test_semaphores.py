import json
import os
import time
from pathlib import Path

import anyio
import pytest

from mh_mcp_server import semaphores
from mh_mcp_server.processes import process_alive, process_created
from support import call, connect, wait_one, write_home
from test_jobs import dead_process

SID = "20261127-100000-0000aa"
SID_B = "20261127-100000-0000bb"


def trees(tmp_path: Path) -> tuple[Path, Path]:
    """`legal` - one resource as a whole; `mh` - a multi-app root: pom-commons.xml at the root, apps/a and apps/b."""
    legal = tmp_path / "legal"
    (legal / "src" / "main").mkdir(parents=True)
    (legal / "pom.xml").write_text("<project/>", encoding="utf-8")
    mh = tmp_path / "mh"
    for app in ("a", "b"):
        (mh / "apps" / app / "src").mkdir(parents=True)
        (mh / "apps" / app / "pom.xml").write_text("<project/>", encoding="utf-8")
    (mh / "pom-commons.xml").write_text("<project/>", encoding="utf-8")
    return legal, mh


def home_with(tmp_path: Path, extra: str = "") -> Path:
    legal, mh = trees(tmp_path)
    return write_home(tmp_path, f'[semaphores]\nmavenDirs = ["{legal.as_posix()}"]\n'
                                f'mavenMultiApp = ["{mh.as_posix()}"]\n{extra}')


# ==================== pure decisions ====================

def test_inside_and_overlaps_ignore_case_and_separators_and_need_a_whole_segment():
    assert semaphores.inside(Path("C:/W/Legal/src"), Path("c:\\w\\legal"))
    assert semaphores.inside(Path("C:/w/legal"), Path("C:/w/legal/"))
    assert not semaphores.inside(Path("C:/w/legal-2"), Path("C:/w/legal"))
    assert semaphores.overlaps(Path("C:/w/mh"), Path("C:/w/mh/apps/a"))
    assert not semaphores.overlaps(Path("C:/w/mh/apps/a"), Path("C:/w/mh/apps/b"))


def test_resource_of_a_dir_is_the_dir_and_under_a_multi_app_root_the_nearest_pom_directory(tmp_path: Path):
    legal, mh = trees(tmp_path)
    of = lambda p: semaphores.resource_of(p, (legal,), (mh,), semaphores._has_pom)
    assert of(legal / "src" / "main") == legal
    assert of(mh / "apps" / "a" / "src") == mh / "apps" / "a"
    assert of(mh / "apps") == mh             # no pom in apps/ itself: the root's pom-commons.xml
    assert of(mh) == mh
    assert of(tmp_path) is None


@pytest.mark.parametrize("command, maven", [
    ("mvn -q test-compile", True),
    ("cd x; mvn.cmd -q surefire:test", True),
    ("./mvnw verify", True),
    ("'mvn'", True),
    ("Get-ChildItem C:/m2/mvn-repo", False),
    ("python -m pytest", False),
    ("npx mvnx", False),
])
def test_is_maven(command, maven):
    assert semaphores.is_maven(command) is maven


def test_tripwire_cwd_inside_a_root_build_above_and_the_path_in_the_text_but_never_its_own(tmp_path: Path):
    legal, mh = trees(tmp_path)
    held = [{"id": "sem-00000001", "type": "MAVEN", "resource": str(mh / "apps" / "a")}]
    roots = (legal, mh)
    assert semaphores.tripwire("mvn test", mh / "apps" / "a" / "src", held, roots, None) is held[0]
    assert semaphores.tripwire("mvn -f pom-commons.xml install", mh, held, roots, None) is held[0]
    assert semaphores.tripwire(f"mvn -f {mh / 'apps' / 'a' / 'pom.xml'} test", tmp_path, held, roots, None) is held[0]
    assert semaphores.tripwire("mvn test", mh / "apps" / "b", held, roots, None) is None
    assert semaphores.tripwire("mvn test", tmp_path, held, roots, None) is None        # tmp_path is not protected
    assert semaphores.tripwire("python -m pytest", mh / "apps" / "a", held, roots, None) is None
    assert semaphores.tripwire("mvn test", mh / "apps" / "a", held, roots, "sem-00000001") is None


def test_is_live_needs_its_ttl_and_its_server():
    me = (os.getpid(), process_created(os.getpid()))
    dead = dead_process()
    record = {"touchedAt": 1000.0, "ttlSeconds": 600, "serverPid": me[0], "serverCreated": me[1]}
    assert semaphores.is_live(record, 1600.0, process_alive)
    assert not semaphores.is_live(record, 1600.5, process_alive)
    assert not semaphores.is_live({**record, "serverPid": dead[0], "serverCreated": dead[1]}, 1001.0, process_alive)


# ==================== through the server ====================

def test_acquire_then_another_session_finds_it_held_and_sees_no_id(tmp_path: Path):
    home = home_with(tmp_path)

    async def main():
        async with connect(home) as session:
            got, _, is_error = await call(session, "acquireSemaphore", sessionId=SID, type="MAVEN",
                                          path=str(tmp_path / "legal" / "src"))
            assert not is_error and got["acquired"] is True, got
            assert got["semaphore"].startswith("sem-") and got["ttlSeconds"] == 600
            assert Path(got["resource"]) == (tmp_path / "legal").resolve()

            held, _, is_error = await call(session, "acquireSemaphore", sessionId=SID_B, type="MAVEN",
                                           path=str(tmp_path / "legal"))
            assert not is_error and held["acquired"] is False, held
            assert held["heldBy"]["sessionId"] == SID and "semaphore" not in held["heldBy"]
            assert 595 <= held["heldBy"]["expiresInSeconds"] <= 600

            mine, _, _ = await call(session, "listSemaphores", sessionId=SID)
            theirs, _, _ = await call(session, "listSemaphores", sessionId=SID_B)
            assert [s["semaphore"] for s in mine["semaphores"]] == [got["semaphore"]]
            assert len(theirs["semaphores"]) == 1 and "semaphore" not in theirs["semaphores"][0]

            released, _, is_error = await call(session, "releaseSemaphore", sessionId=SID, semaphore=got["semaphore"])
            assert not is_error and released["released"] is True and released["jobs"] == []
            again, _, is_error = await call(session, "releaseSemaphore", sessionId=SID, semaphore=got["semaphore"])
            assert is_error and again["error"] == "SEMAPHORE_NOT_FOUND"
            fresh, _, _ = await call(session, "acquireSemaphore", sessionId=SID_B, type="MAVEN", path=str(tmp_path / "legal"))
            assert fresh["acquired"] is True

    anyio.run(main)


def test_multi_app_apps_are_separate_resources_and_the_root_overlaps_both(tmp_path: Path):
    home = home_with(tmp_path)
    apps = tmp_path / "mh" / "apps"

    async def main():
        async with connect(home) as session:
            a, _, _ = await call(session, "acquireSemaphore", sessionId=SID, type="MAVEN", path=str(apps / "a" / "src"))
            b, _, _ = await call(session, "acquireSemaphore", sessionId=SID_B, type="MAVEN", path=str(apps / "b"))
            assert a["acquired"] and b["acquired"], (a, b)
            assert Path(a["resource"]) == (apps / "a").resolve() and Path(b["resource"]) == (apps / "b").resolve()
            root, _, _ = await call(session, "acquireSemaphore", sessionId=SID_B, type="MAVEN", path=str(tmp_path / "mh"))
            assert root["acquired"] is False and Path(root["resource"]) == (tmp_path / "mh").resolve()

    anyio.run(main)


def test_wait_seconds_acquires_once_the_holders_ttl_runs_out(tmp_path: Path):
    home = home_with(tmp_path)

    async def main():
        async with connect(home) as session:
            first, _, _ = await call(session, "acquireSemaphore", sessionId=SID, type="MAVEN",
                                     path=str(tmp_path / "legal"), ttlSeconds=2)
            assert first["acquired"]
            started = time.monotonic()
            second, _, _ = await call(session, "acquireSemaphore", sessionId=SID_B, type="MAVEN",
                                      path=str(tmp_path / "legal"), waitSeconds=10)
            assert second["acquired"] is True and second["semaphore"] != first["semaphore"], second
            assert 1.0 <= time.monotonic() - started < 8 and second["waited"] >= 1.0
            gone, _, is_error = await call(session, "releaseSemaphore", sessionId=SID, semaphore=first["semaphore"])
            assert is_error and gone["error"] == "SEMAPHORE_NOT_FOUND"

    anyio.run(main)


def test_a_semaphore_of_a_dead_server_is_dropped(tmp_path: Path):
    home = home_with(tmp_path)
    dead = dead_process()
    store = semaphores.store_path(home)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"semaphores": [{
        "id": "sem-0000dead", "type": "MAVEN", "resource": str((tmp_path / "legal").resolve()), "sessionId": SID_B,
        "serverPid": dead[0], "serverCreated": dead[1], "acquiredAt": time.time(), "touchedAt": time.time(),
        "ttlSeconds": 600, "jobs": []}]}), encoding="utf-8")

    async def main():
        async with connect(home) as session:
            got, _, _ = await call(session, "acquireSemaphore", sessionId=SID, type="MAVEN", path=str(tmp_path / "legal"))
            assert got["acquired"] is True, got

    anyio.run(main)
    assert [r["sessionId"] for r in json.loads(store.read_text(encoding="utf-8"))["semaphores"]] == [SID]


def test_acquire_refusals(tmp_path: Path):
    home = home_with(tmp_path)
    (tmp_path / "web").mkdir()

    async def main():
        async with connect(home) as session:
            r, _, is_error = await call(session, "acquireSemaphore", sessionId=SID, type="MAVEN", path=str(tmp_path / "web"))
            assert is_error and r["error"] == "NOT_PROTECTED", r
            r, _, _ = await call(session, "acquireSemaphore", sessionId=SID, type="MAVEN", path=str(tmp_path / "absent"))
            assert r["error"] == "NOT_FOUND"
            r, _, _ = await call(session, "acquireSemaphore", sessionId=SID, type="NPM", path=str(tmp_path / "legal"))
            assert r["error"] == "BAD_ARGUMENTS"
            r, _, _ = await call(session, "acquireSemaphore", sessionId=SID, type="MAVEN", path=str(tmp_path / "legal"),
                                 ttlSeconds=3601)
            assert r["error"] == "BAD_ARGUMENTS"
            r, _, _ = await call(session, "acquireSemaphore", sessionId=SID, type="MAVEN", path=str(tmp_path / "legal"),
                                 waitSeconds=59)
            assert r["error"] == "BAD_ARGUMENTS"

    anyio.run(main)


def test_tripwire_refuses_a_non_holders_mvn_and_lets_the_holder_and_other_commands_run(tmp_path: Path):
    home = home_with(tmp_path)
    legal = str(tmp_path / "legal")

    async def main():
        async with connect(home) as session:
            got, _, _ = await call(session, "acquireSemaphore", sessionId=SID, type="MAVEN", path=legal)
            refused, _, is_error = await call(session, "executeCommand", sessionId=SID_B, command="'mvn -q test'", cwd=legal)
            assert is_error and refused["error"] == "SEMAPHORE_HELD" and refused["heldBy"]["sessionId"] == SID, refused
            refused, _, is_error = await call(session, "startJob", sessionId=SID_B, command="'mvn -q test'", cwd=legal)
            assert is_error and refused["error"] == "SEMAPHORE_HELD"
            header, _, _ = await call(session, "getSession", sessionId=SID_B)
            assert header["jobs"] == []                              # nothing was run
            header, text, is_error = await call(session, "executeCommand", sessionId=SID_B, command="'other'", cwd=legal)
            assert not is_error and text == "other"
            header, text, is_error = await call(session, "executeCommand", sessionId=SID_B, command="'mvn -q test'",
                                                cwd=legal, semaphore=got["semaphore"])
            assert not is_error and text == "mvn -q test", header
            header, _, is_error = await call(session, "startJob", sessionId=SID_B, command="'x'", semaphore="sem-00000000")
            assert is_error and header["error"] == "SEMAPHORE_NOT_FOUND"

    anyio.run(main)


def test_a_job_started_with_the_id_joins_it_and_wait_job_keeps_it_alive(tmp_path: Path):
    home = home_with(tmp_path)

    async def main():
        async with connect(home) as session:
            got, _, _ = await call(session, "acquireSemaphore", sessionId=SID, type="MAVEN",
                                   path=str(tmp_path / "legal"), ttlSeconds=3)
            job, _, is_error = await call(session, "startJob", sessionId=SID_B, command="Start-Sleep 7; 'done'",
                                          semaphore=got["semaphore"])
            assert not is_error, job
            for _ in range(3):                                       # ~6 s of waits, refreshed every ~2 s - TTL 3 s
                await wait_one(session, SID_B, job["jobId"], maxSeconds=2)
            listed, _, _ = await call(session, "listSemaphores", sessionId=SID)
            assert [s["semaphore"] for s in listed["semaphores"]] == [got["semaphore"]], listed
            assert listed["semaphores"][0]["jobs"][0]["jobId"] == job["jobId"]
            header, _, _ = await wait_one(session, SID_B, job["jobId"], maxSeconds=30)
            assert header["state"] == "done"
            released, _, _ = await call(session, "releaseSemaphore", sessionId=SID, semaphore=got["semaphore"])
            assert released["jobs"] == [{"sessionId": SID_B, "jobId": job["jobId"], "state": "done"}]

    anyio.run(main)


def test_disabled_mh_mcp_works_as_0_1_14_the_id_is_ignored_and_nothing_is_stored(tmp_path: Path):
    home = home_with(tmp_path, "enabled = false\n")

    async def main():
        async with connect(home) as session:
            header, text, is_error = await call(session, "executeCommand", sessionId=SID, command="'mvn -q test'",
                                                cwd=str(tmp_path / "legal"), semaphore="sem-00000000")
            assert not is_error and text == "mvn -q test", header
            job, _, is_error = await call(session, "startJob", sessionId=SID, command="'x'", semaphore="sem-00000000")
            assert not is_error, job
            await wait_one(session, SID, job["jobId"], maxSeconds=30)

    anyio.run(main)
    assert not semaphores.store_path(home).exists()
