"""Semaphores over shared build resources (0.1.15).

Two chats building one Maven tree at once break each other: the second `mvn` rewrites target/ under the first one's
JVM. A semaphore names one resource of one type - MAVEN, so far - and is held by whoever has its id: the chat that
acquired it passes the id to startJob / executeCommand for its runs, whichever session each run uses.

Which resources (config [semaphores]): a `mavenDirs` entry is one resource as a whole; under a `mavenMultiApp` root
every directory holding a pom.xml or pom-commons.xml is its own resource, the nearest one at or above the path. Two
resources of one type conflict when one lies inside the other - a root build covers the apps under it.

Where: state/semaphores.json in the home, shared by every server process of the home (Desktop runs more than one), and
changed only under state/semaphores.json.mutex. A semaphore lives while the server process that created it lives and
until ttlSeconds pass without use: a dead or expired one is dropped by the next semaphore operation of any server, so
nothing outlives the server that made it. Use is: acquiring it, a startJob / executeCommand carrying its id, a waitJob
on a job such a call started.

Tripwire: an `mvn` command whose working directory or text reaches a resource another semaphore holds is refused
SEMAPHORE_HELD - a guard against a chat that forgot to acquire, not a sandbox: a path built at run time is not seen.

config [semaphores] enabled = false: the semaphore tools are not registered and nothing here runs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import anyio

from mh_mcp_server.calls import call_state, runs_dir
from mh_mcp_server.execution import Reply, Runtime, refusal
from mh_mcp_server.processes import process_created
from mh_mcp_server.session_id import is_valid_session_id
from mh_mcp_server.session_state import STATE_DIR, _read_json, _remove_quietly, _write_json_atomic, load_state

log = logging.getLogger("mh_mcp_server.semaphores")

TYPES = ("MAVEN",)
POM_FILES = ("pom.xml", "pom-commons.xml")
STORE_FILE = "semaphores.json"
TTL_MAX = 3600
WAIT_POLL_SECONDS = 0.5
_MUTEX_SECONDS = 5.0
# mvn, mvnw, mvn.cmd, mvnw.cmd as a word of the command - not a part of a longer name or of a path
_MVN = re.compile(r"(?<![\w.-])mvnw?(?:\.cmd|\.bat)?(?![\w.-])", re.IGNORECASE)


# ==================== pure decisions ====================

def path_key(path: Path) -> str:
    """A path as compared here: forward slashes, no trailing slash, case-folded (Windows paths are case-blind)."""
    return str(path).replace("\\", "/").rstrip("/").casefold()


def inside(path: Path, root: Path) -> bool:
    """`path` is `root` or lies under it."""
    p, r = path_key(path), path_key(root)
    return p == r or p.startswith(r + "/")


def overlaps(a: Path, b: Path) -> bool:
    return inside(a, b) or inside(b, a)


def resource_of(path: Path, dirs: Sequence[Path], multi_app: Sequence[Path],
                has_pom: Callable[[Path], bool]) -> Path | None:
    """The protected resource `path` belongs to, the deepest of: a `dirs` entry holding it; under a `multi_app` root
    holding it, the nearest directory at or above `path` with a pom. None when `path` is not protected."""
    found: list[Path] = [d for d in dirs if inside(path, d)]
    for root in multi_app:
        if not inside(path, root):
            continue
        probe = path
        while inside(probe, root):
            if has_pom(probe):
                found.append(probe)
                break
            if probe.parent == probe:
                break
            probe = probe.parent
    return max(found, key=lambda p: len(path_key(p))) if found else None


def is_live(record: Mapping[str, Any], now: float, alive: Callable[[int, int | None], bool]) -> bool:
    """Used within its TTL, and its server process still running."""
    return now - record["touchedAt"] <= record["ttlSeconds"] and alive(record["serverPid"], record["serverCreated"])


def is_maven(command: str) -> bool:
    return _MVN.search(command) is not None


def tripwire(command: str, cwd: Path, held: Sequence[Mapping[str, Any]], protected_roots: Sequence[Path],
             own: str | None) -> Mapping[str, Any] | None:
    """The semaphore an `mvn` command would run into - its cwd inside a held resource; a held resource inside its cwd
    when that cwd lies in a protected tree (a root build); or the held resource's path in its text. Never `own`."""
    if not is_maven(command):
        return None
    text = command.replace("\\", "/").casefold()
    for record in held:
        if record["id"] == own or record["type"] != "MAVEN":
            continue
        resource = Path(record["resource"])
        if inside(cwd, resource):
            return record
        if inside(resource, cwd) and any(inside(cwd, root) for root in protected_roots):
            return record
        if path_key(resource) in text:
            return record
    return None


# ==================== the store ====================

def store_path(home: Path) -> Path:
    return home / STATE_DIR / STORE_FILE


@contextmanager
def _mutex(path: Path, alive: Callable[[int, int | None], bool]) -> Iterator[None]:
    """`<store>.mutex` created with O_EXCL and naming its process; a mutex whose process is dead is taken over."""
    mutex = path.with_name(path.name + ".mutex")
    mutex.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _MUTEX_SECONDS
    while True:
        try:
            fd = os.open(mutex, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
            break
        except FileExistsError:
            owner = _read_json(mutex)
            if owner is not None and not alive(owner["pid"], owner["created"]):
                _remove_quietly(mutex)
        except PermissionError:
            pass  # delete-pending on Windows
        if time.monotonic() > deadline:
            raise TimeoutError(f"{mutex} held for more than {_MUTEX_SECONDS} s")
        time.sleep(0.002)
    try:
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "created": process_created(os.getpid())}).encode("utf-8"))
        finally:
            os.close(fd)
        yield
    finally:
        _remove_quietly(mutex)


def _change(rt: Runtime, change: Callable[[list[dict], float], Any]) -> Any:
    """Runs `change(live, now)` over the live semaphores under the mutex - dead and expired ones dropped first, and
    logged - and writes the store when anything differs from what was read."""
    path = store_path(rt.home)
    with _mutex(path, rt.alive):
        read = (_read_json(path) or {}).get("semaphores", [])
        now = time.time()
        live = [r for r in read if is_live(r, now, rt.alive)]
        for r in read:
            if r not in live:
                why = "expired" if now - r["touchedAt"] > r["ttlSeconds"] else "its server is gone"
                log.info("semaphore %s %s on %s dropped: %s", r["id"], r["type"], r["resource"], why)
        before = json.dumps(live, sort_keys=True)
        result = change(live, now)
        if len(live) != len(read) or json.dumps(live, sort_keys=True) != before:
            _write_json_atomic(path, {"semaphores": live})
        return result


def _public(rt: Runtime, record: Mapping[str, Any], now: float, caller: str | None) -> dict[str, Any]:
    """What any session may see of a semaphore - its id only for the session that acquired it."""
    view: dict[str, Any] = {}
    if caller is not None and caller == record["sessionId"]:
        view["semaphore"] = record["id"]
    view.update({
        "type": record["type"],
        "resource": record["resource"],
        "sessionId": record["sessionId"],
        "acquiredAt": _iso(record["acquiredAt"]),
        "touchedAt": _iso(record["touchedAt"]),
        "expiresInSeconds": max(0, round(record["ttlSeconds"] - (now - record["touchedAt"]))),
        "jobs": [{"sessionId": sid, "jobId": job_id, "state": call_state(runs_dir(rt.home, sid) / job_id, rt.alive)}
                 for sid, job_id in record["jobs"]],
    })
    return view


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _resolve(rt: Runtime, session_id: str, path: str | None) -> Path:
    """`path` - the session's cwd when None - made absolute, a relative one taken from the session's cwd."""
    base = load_state(rt.home, session_id, rt.config.default_cwd).cwd
    return (base if path is None else base / path).resolve()


def _protected(rt: Runtime) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    return tuple(p.resolve() for p in rt.config.maven_dirs), tuple(p.resolve() for p in rt.config.maven_multi_app)


def _has_pom(path: Path) -> bool:
    return any((path / name).is_file() for name in POM_FILES)


# ==================== the tools ====================

async def acquire(rt: Runtime, session_id: str, type_: str, path: str, ttl_seconds: int | None = None,
                  wait_seconds: int = 0) -> Reply:
    if not is_valid_session_id(session_id):
        return refusal("BAD_SESSION_ID")
    if type_ not in TYPES:
        return refusal("BAD_ARGUMENTS", session_id, message=f"type: one of {', '.join(TYPES)}")
    ttl = rt.config.semaphore_ttl_seconds if ttl_seconds is None else ttl_seconds
    if not 1 <= ttl <= TTL_MAX or not 0 <= wait_seconds <= rt.config.max_foreground_seconds:
        return refusal("BAD_ARGUMENTS", session_id,
                       message=f"ttlSeconds 1 - {TTL_MAX}, waitSeconds 0 - {rt.config.max_foreground_seconds}")
    target = await anyio.to_thread.run_sync(_resolve, rt, session_id, path)
    if not target.exists():
        return refusal("NOT_FOUND", session_id, path=str(target))
    dirs, multi_app = _protected(rt)
    resource = await anyio.to_thread.run_sync(resource_of, target, dirs, multi_app, _has_pom)
    if resource is None:
        return refusal("NOT_PROTECTED", session_id, type=type_, path=str(target),
                       message="not a protected resource - no semaphore is needed, run without one")

    def once(live: list[dict], now: float) -> dict[str, Any]:
        holder = next((r for r in live if r["type"] == type_ and overlaps(Path(r["resource"]), resource)), None)
        if holder is not None:
            return {"acquired": False, "type": type_, "resource": str(resource),
                    "heldBy": _public(rt, holder, now, session_id)}
        record = {"id": "sem-" + secrets.token_hex(4), "type": type_, "resource": str(resource),
                  "sessionId": session_id, "serverPid": rt.server[0], "serverCreated": rt.server[1],
                  "acquiredAt": now, "touchedAt": now, "ttlSeconds": ttl, "jobs": []}
        live.append(record)
        log.info("semaphore %s %s on %s acquired by session %s", record["id"], type_, resource, session_id)
        return {"acquired": True, "semaphore": record["id"], "type": type_, "resource": str(resource),
                "ttlSeconds": ttl}

    started = time.monotonic()
    while True:
        result = await anyio.to_thread.run_sync(_change, rt, once)
        remaining = wait_seconds - (time.monotonic() - started)
        if result["acquired"] or remaining <= 0:
            break
        await anyio.sleep(min(WAIT_POLL_SECONDS, remaining))
    result["waited"] = round(time.monotonic() - started, 2)
    return Reply(result, session_id=session_id)


def release(rt: Runtime, session_id: str, semaphore: str) -> Reply:
    if not is_valid_session_id(session_id):
        return refusal("BAD_SESSION_ID")

    def drop(live: list[dict], now: float) -> dict[str, Any] | None:
        record = next((r for r in live if r["id"] == semaphore), None)
        if record is None:
            return None
        live.remove(record)
        log.info("semaphore %s %s on %s released by session %s", semaphore, record["type"], record["resource"],
                 session_id)
        view = _public(rt, record, now, None)
        return {"released": True, "type": view["type"], "resource": view["resource"], "jobs": view["jobs"]}

    result = _change(rt, drop)
    if result is None:
        return refusal("SEMAPHORE_NOT_FOUND", session_id, semaphore=semaphore,
                       message="no such semaphore - never acquired, released, or expired")
    return Reply(result, session_id=session_id)


def list_semaphores(rt: Runtime, session_id: str) -> Reply:
    if not is_valid_session_id(session_id):
        return refusal("BAD_SESSION_ID")
    views = _change(rt, lambda live, now: [_public(rt, r, now, session_id) for r in live])
    dirs, multi_app = _protected(rt)
    return Reply({"semaphores": views,
                  "protected": {"MAVEN": {"dirs": [str(p) for p in dirs], "multiApp": [str(p) for p in multi_app]}},
                  "ttlSeconds": rt.config.semaphore_ttl_seconds}, session_id=session_id)


def gate(rt: Runtime, session_id: str, command: str, cwd: str | None, semaphore: str | None) -> Reply | None:
    """Before executeCommand / startJob: a refusal when `semaphore` is not live, or when the command is an `mvn` that
    runs into a resource another semaphore holds; otherwise None - and `semaphore` is refreshed."""
    if not is_valid_session_id(session_id):
        return None  # the tool itself refuses it
    if semaphore is None and not is_maven(command):
        return None  # nothing to check and nothing to refresh: the store is not touched
    try:
        target = _resolve(rt, session_id, cwd)
    except OSError:
        return None  # the tool itself reports the cwd
    dirs, multi_app = _protected(rt)

    def check(live: list[dict], now: float) -> Reply | None:
        own = None
        if semaphore is not None:
            own = next((r for r in live if r["id"] == semaphore), None)
            if own is None:
                return refusal("SEMAPHORE_NOT_FOUND", session_id, semaphore=semaphore,
                               message="no such semaphore - never acquired, released, or expired; nothing was run")
        hit = tripwire(command, target, live, (*dirs, *multi_app), semaphore)
        if hit is not None:
            return refusal("SEMAPHORE_HELD", session_id, type=hit["type"], resource=hit["resource"],
                           heldBy=_public(rt, hit, now, session_id),
                           message="another chat holds this resource: acquireSemaphore (waitSeconds), pass its id")
        if own is not None:
            own["touchedAt"] = now
        return None

    return _change(rt, check)


def joined(rt: Runtime, semaphore: str | None, session_id: str, reply: Reply) -> None:
    """After executeCommand / startJob with `semaphore`: a job the call left running joins the semaphore."""
    job_id = reply.header.get("jobId")
    if semaphore is None or reply.is_error or job_id is None:
        return

    def add(live: list[dict], now: float) -> None:
        record = next((r for r in live if r["id"] == semaphore), None)
        if record is not None:
            if [session_id, job_id] not in record["jobs"]:
                record["jobs"].append([session_id, job_id])
            record["touchedAt"] = now

    _change(rt, add)


def touch_jobs(rt: Runtime, jobs: Sequence[tuple[str, str]]) -> None:
    """After a waitJob: every semaphore one of the waited jobs belongs to is refreshed."""
    wanted = {(sid, job_id) for sid, job_id in jobs}

    def touch(live: list[dict], now: float) -> None:
        for record in live:
            if any((sid, job_id) in wanted for sid, job_id in record["jobs"]):
                record["touchedAt"] = now

    _change(rt, touch)
