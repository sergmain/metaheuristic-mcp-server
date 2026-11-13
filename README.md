# metaheuristic-mcp-server

A stdio MCP server, launched by Claude Desktop, that gives a chat executor pwsh commands and exact file reading,
writing and editing through sessions identified by a `sessionId`. Every command runs in its own `pwsh` process under
a detached supervisor that writes the output and the exit code to files under the runtime home
(`MH_MCP_SERVER_HOME`), so any number of chats can use one server at once, a command that outlives its call
continues as a job, and the server holds no state in memory that a call depends on. Every request and response is
recorded in a UTF-8 transcript per session.

Since 0.1.9 `getMemory(sessionId)` reports the box's memory from one in-process `GlobalMemoryStatusEx` call - used,
available and total physical memory, the memory load, and the commit charge (used, free, limit), in GiB - so a chat
watching memory no longer starts a `pwsh` process to query WMI. Windows only: elsewhere it refuses with
`MEMORY_UNAVAILABLE`.

Since 0.1.10 three file tools answer in process what chats most often ran as `pwsh` commands (mh-mcp's own transcripts,
2026-10-23 - 10-26: 818 greps, 380 directory listings, 162 line / byte counts): `searchFiles` (a regex over a tree or a
file, `path:line: text`), `listFiles` (a listing, or a find with a glob and `maxDepth`) and `fileInfo` (lines, bytes, line
ending for up to 50 files). They obey readFile's roots and `[excluded]`, never follow a link or a junction, and skip
`.git`, `target`, `node_modules`, `__pycache__`, `.idea`, `.venv`, `dist`, `.pytest_cache` unless asked.

Since 0.1.11 `cancelJob(sessionId, jobId)` ends a running job: it writes `cancel.json` beside the call, then kills the
job's pwsh and every process it started (`taskkill /T /F`, only while that pwsh is still the process `pid.json` recorded,
by its creation time). The supervisor survives and writes `exit.json`, so the job ends `done` and `waitJob` reports
`cancelled`.

Since 0.1.12 two tools replace the chats' most frequent remaining commands that need care rather than speed (mh-mcp's
transcripts, 2026-10-27 - 11-09: 726 `psql` commands, 353 commits / pushes):

- `sqlQuery(sessionId, sql, database?, maxRows?, timeoutSeconds?)` runs SQL through psql on stdin, as UTF-8 (a `-c`
  argument loses every character outside the Windows ANSI code page). The first stdin line is `\restrict <key>` with a
  random key per call, so psql refuses every backslash command (`\!`, `\o`, `\copy`) and the SQL cannot leave the
  database. It connects as `[sql] user`, a SELECT-only role (`pg_read_all_data`), with `default_transaction_read_only`
  and a `statement_timeout` before the call's deadline. The role is the guard SQL text cannot lift. The whole result is
  `output.csv` in the call's directory; the reply shows the first `maxRows` rows as CSV, NULL as `<NULL>`.
- `gitCommit(sessionId, repo, paths, message, push?, timeoutSeconds?)` commits exactly the named files: `git add --
  <paths>`, then `git commit -F - -- <paths>`, then `git push origin HEAD`. git runs with argv and the message goes on
  stdin, so nothing is quoted. Another path's staged change stays out of the commit, and a deleted file commits as a
  deletion. The repository itself, a directory and a wildcard are refused as paths.

Both run their child process inside the call (`children.py`): stdin is closed at once, and a child still running at
the deadline is killed and reported `timedOut`.

Since 0.1.13 three tools take several targets in one call, because every round trip re-reads the chat's whole context
(plan 056): `waitJob(jobs, mode?, maxSeconds?, untilRegex?, maxLines?)` waits on 1-20 jobs, each named by its
`sessionId` and `jobId` with its own `sinceLine`, until any (`mode="any"`) or every (`"all"`) job is done, lost or
matched; it has no sessionId of its own and its transcript block goes to every session it names. `searchFiles` takes
`roots` (1-20, paths relative to their deepest common directory, `base`) and `outputMode` `lines` / `files` / `count`.
`readFile` takes `ranges` (1-20 `{path, fromLine, toLine}`) instead of `path`, sharing one read budget. No
compatibility form is kept: one server, one consumer. Every transcript block's head line also carries the bytes the
client received and the server version: `=== <at> | call <id> | <tool> | <s> s | <bytes> B | <version>`.

The sections up to [Tests](#tests) hold what does not depend on the OS; [Windows](#windows) and [Linux](#linux) hold
the configuration of each.

## Release

A release is the git tag `v<version>` of the clone (equal to `__version__` of that commit), extracted once into its
own directory `dist/<version>/` of the runtime home - never inside the clone. Claude Desktop's config points
`PYTHONPATH` at the link `dist/prod`, which points at the current `<version>/src`; a new release is a new directory,
the link repointed and a Desktop restart; rollback is repointing it back. A release directory is never edited,
re-extracted or checked out in place: `supervisor.py` is started by path on every call, so a server running from the
clone, or a new version checked out under a running server, would mix versions or break every command at once.
Repointing `prod` under a running server is safe: the server resolves its own path through the link at import
(`Path(__file__).resolve()`), so it keeps starting the `supervisor.py` of the version it loaded until it exits;
`getSessionId`'s `serverRoot` names that real directory.

The link is a junction on Windows and a symbolic link on Linux; the commands are in each OS section.

## Runtime home

`MH_MCP_SERVER_HOME` names the runtime directory: `config.toml`, `sessions/`, `state/`, `runs/`, `logs/`,
and the releases in `dist/<version>/`. Clearing state means `sessions/`, `state/` and `runs/` - never `dist/`, which
holds the running release. How the variable reaches the server is OS-specific.

`logs/` holds one file per server instance, `server-<sid>.log`, where `<sid>` is minted like a sessionId at the
instance's start (`yyyyMMdd-HHmmss-<6 hex>`), so the instances Claude Desktop starts side by side never share a file;
every line also names the instance's `[pid N]`. A starting instance keeps the newest 20 (its own included) and skips
one another instance still holds open. The `server.log` of releases before 0.1.7 is left alone.

Since 0.1.8 the instance's file (only the file, not stderr) also gets one line when a `tools/call` arrives and one when
it ends, with its JSON-RPC request id, tool and sessionId, and one line for each `notifications/cancelled`, which the
server ignores (python-sdk#2610). A client timeout with no arrival line was never sent to the server.

## Configuration

`config.toml` in the runtime home, over the built-in defaults (`config.DEFAULT_CONFIG_TOML`); an unknown table or key
is an error. The defaults are the Windows layout (`[execution] defaultCwd`, `[paths] writeRoots` and `readRoots` under
`C:/sandbox/_work`), so a Linux home overrides them.

`[excluded] paths` lists starts of absolute paths the file tools (`readFile`, `writeFile`, `editFile`) refuse with
`PATH_EXCLUDED`, even inside `[paths]` roots - a plain string prefix, case-insensitive, `/` and `\` alike, so an item
also covers every sibling that starts with the same text (`C:\Windows` covers `C:\WindowsApps`) and an item ending in
a separator covers that directory only (`C:\Windows\`).

`executeCommand` and `startJob` refuse with `PATH_EXCLUDED` too (since 0.1.5): `"where": "command"` when the command
text contains an item - the same normalization, and a run of separators counts as one (`C:\\Windows`, `C://Windows`) -
and `"where": "cwd"` when the directory the command would run in, the given `cwd` or the session's own, lies in one.
On the command path only a path written out in the text is seen; one built at run time (`$env:windir`, `Join-Path`,
`cd ..` followed by a name) is not, so this check is a tripwire, not a sandbox - only the file tools' check is complete.

`[sql]` (since 0.1.12) is sqlQuery's connection: `psql` (found on the command PATH), `host`, `port`, `user`, `database`
(the default; a call may name another) and `passwordVariable`, the name of the command-environment variable that holds
the password. The password itself goes into `[env]` and its name into `[secrets]`, so it is masked like every secret;
`[sql]` has no password key. The defaults (`localhost`, `5432`, `mh_mcp_ro`, `mh`, `MH_MCP_SQL_PASSWORD`) fit the dev
box, so a home normally has no `[sql]` table at all.

## Tests

`python -m pytest` in the clone - real files, real `pwsh`, real processes, no mocks. The stdio tests start the server
from the clone, never Desktop. `tests/test_sql.py` runs against the box's PostgreSQL: it creates a throwaway SELECT-only
role (allowed the reserved slots, like `mh_mcp_ro`) and table in `postgres` with the superuser password from `PGPASSWORD`
(mh-mcp's own `[env]` provides it to a run started through mh-mcp), and drops both at the end; without psql or that
password those tests are skipped.
`tests/test_git_commit.py` builds its repositories in `tmp_path`.

## Windows

Paths: the clone `C:\sandbox\_work\metaheuristic-mcp-server`, the runtime home `C:\_mh\mh-mcp-server`.

### Runtime home

`MH_MCP_SERVER_HOME` is an OS environment variable, `C:\_mh\mh-mcp-server`. The server reads it from its own process
environment, then from the user's (`HKCU\Environment`), then from the machine's; without the variable the server
writes one line to stderr and exits 2.

### Release

```
git -C C:\sandbox\_work\metaheuristic-mcp-server tag v0.1.2
git -C C:\sandbox\_work\metaheuristic-mcp-server push origin v0.1.2
New-Item -ItemType Directory -Force C:\_mh\mh-mcp-server\dist | Out-Null
git -C C:\sandbox\_work\metaheuristic-mcp-server archive --format=zip -o C:\_mh\mh-mcp-server\dist\v0.1.2.zip v0.1.2 src
Expand-Archive C:\_mh\mh-mcp-server\dist\v0.1.2.zip C:\_mh\mh-mcp-server\dist\0.1.2
# first time: create the junction; later releases: repoint it
New-Item -ItemType Junction -Path C:\_mh\mh-mcp-server\dist\prod -Target C:\_mh\mh-mcp-server\dist\0.1.2\src
# repoint: remove only the junction (never -Recurse, which would follow it), then create it again
(Get-Item C:\_mh\mh-mcp-server\dist\prod).Delete()
New-Item -ItemType Junction -Path C:\_mh\mh-mcp-server\dist\prod -Target C:\_mh\mh-mcp-server\dist\<version>\src
```

`Expand-Archive` refuses an existing destination, which is the point: a release directory is created once. A
junction, unlike a symbolic link, needs neither administrator rights nor Developer Mode.

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json` - set once, never changed by a release:

```
"mh-mcp": {
  "command": "C:\\anaconda3\\envs\\python_3-13\\python.exe",
  "args": ["-m", "mh_mcp_server"],
  "env": { "PYTHONPATH": "C:\\_mh\\mh-mcp-server\\dist\\prod" }
}
```

### config.toml

`C:\_mh\mh-mcp-server\config.toml`. The defaults already fit this layout, so the file holds only what differs from
them:

```
[excluded]
paths = ["C:\\Windows"]
```

sqlQuery's password (since 0.1.12) is one more `[env]` variable, listed in `[secrets]`:

```
[env]
MH_MCP_SQL_PASSWORD = "<the password of mh_mcp_ro>"

[secrets]
names = ["PGPASSWORD", "NRVV_REST_AUTH", "MH_MCP_SQL_PASSWORD"]
```

The role, created once as the superuser:

```
create role mh_mcp_ro login password '<password>';
grant pg_read_all_data to mh_mcp_ro;
grant pg_use_reserved_connections to mh_mcp_ro;
alter role mh_mcp_ro set default_transaction_read_only = on;
```

`pg_use_reserved_connections` matters only once the server's `reserved_connections` is above 0. Without it, the role
cannot connect while other clients hold every non-superuser slot (`max_connections` minus
`superuser_reserved_connections`); psql then exits 2 with "remaining connection slots are reserved".

The dev box reserves 3 slots for it (2026-11-09: MH's pool held 97 of 100, every slot a non-superuser could take):

```
ALTER SYSTEM SET max_connections = 103;
ALTER SYSTEM SET reserved_connections = 3;
-- both are postmaster settings: they take effect at the next restart of the service (elevated)
Restart-Service postgresql-x64-18
```

Ordinary roles keep 103 - 3 - 3 = 97 slots, as before, so MH's pool is unchanged; `mh_mcp_ro` (and the tests' throwaway
role) may use 3 more of its own; superusers keep their 3.

### Tests

`cd C:\sandbox\_work\metaheuristic-mcp-server; python -m pytest` - `--basetemp` is
`C:/sandbox/_work/temp/mh-mcp-pytest` (`pyproject.toml`).

## Linux

Not run on Linux yet: this setup mirrors the Windows one and may need corrections.

Paths (examples, mirroring Windows): the clone `~/sandbox/_work/metaheuristic-mcp-server`, the runtime home
`~/_mh/mh-mcp-server`. JSON and TOML expand neither `~` nor `$HOME`, so the files below spell the home directory out
as `/home/<user>`.

Prerequisites: Python 3.13 or later with the `mcp` package (`pyproject.toml`), PowerShell 7 installed as `pwsh`.

### Runtime home

There is no registry to fall back to, so `MH_MCP_SERVER_HOME` goes into the `env` of the Claude Desktop entry below,
which the server always sees - not into a shell profile, which a Desktop started from the application launcher does
not read.

```
mkdir -p ~/_mh/mh-mcp-server
```

### Release

```
git -C ~/sandbox/_work/metaheuristic-mcp-server tag v0.1.2
git -C ~/sandbox/_work/metaheuristic-mcp-server push origin v0.1.2
mkdir -p ~/_mh/mh-mcp-server/dist
# no -p here: mkdir refuses an existing release directory
mkdir ~/_mh/mh-mcp-server/dist/0.1.2
git -C ~/sandbox/_work/metaheuristic-mcp-server archive --format=tar v0.1.2 src | tar -x -C ~/_mh/mh-mcp-server/dist/0.1.2
# first time and every later release: -n replaces the link itself instead of creating a new one inside the old target
ln -sfn ~/_mh/mh-mcp-server/dist/0.1.2/src ~/_mh/mh-mcp-server/dist/prod
```

`mkdir` without `-p` refuses an existing directory, which plays the part of `Expand-Archive` on Windows: a release
directory is created once. The same `ln -sfn` with an older `<version>` is the rollback.

### Claude Desktop

Claude Desktop for Linux (beta, Ubuntu and Debian): `~/.config/Claude/claude_desktop_config.json` (Settings ->
Developer -> Edit Config opens it) - set once, never changed by a release:

```
"mh-mcp": {
  "command": "/usr/bin/python3.13",
  "args": ["-m", "mh_mcp_server"],
  "env": {
    "PYTHONPATH": "/home/<user>/_mh/mh-mcp-server/dist/prod",
    "MH_MCP_SERVER_HOME": "/home/<user>/_mh/mh-mcp-server"
  }
}
```

`command` is the full path of a Python 3.13+ interpreter that has `mcp` installed, as on Windows.

### config.toml

`~/_mh/mh-mcp-server/config.toml`. The defaults are Windows paths, so the working directory and the roots are set
here:

```
[execution]
defaultCwd = "/home/<user>/sandbox/_work"

[paths]
writeRoots = ["/home/<user>/sandbox/_work"]
readRoots = ["/home/<user>/sandbox/_work"]

[excluded]
paths = ["/etc/"]
```

### Tests

`cd ~/sandbox/_work/metaheuristic-mcp-server && python -m pytest --basetemp=$HOME/sandbox/_work/temp/mh-mcp-pytest` -
the `--basetemp` of `pyproject.toml` is a Windows path, and the one given on the command line wins.
