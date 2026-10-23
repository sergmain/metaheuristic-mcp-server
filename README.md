# metaheuristic-mcp-server

A stdio MCP server, launched by Claude Desktop, that gives a chat executor pwsh commands and exact file reading,
writing and editing through sessions identified by a `sessionId`. Every command runs in its own `pwsh` process under
a detached supervisor that writes the output and the exit code to files under the runtime home
(`MH_MCP_SERVER_HOME`), so any number of chats can use one server at once, a command that outlives its call
continues as a job, and the server holds no state in memory that a call depends on. Every request and response is
recorded in a UTF-8 transcript per session.

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

`MH_MCP_SERVER_HOME` names the runtime directory: `config.toml`, `sessions/`, `state/`, `runs/`, `logs/server.log`,
and the releases in `dist/<version>/`. Clearing state means `sessions/`, `state/` and `runs/` - never `dist/`, which
holds the running release. How the variable reaches the server is OS-specific.

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

## Tests

`python -m pytest` in the clone - real files, real `pwsh`, real processes, no mocks. The stdio tests start the server
from the clone, never Desktop.

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
