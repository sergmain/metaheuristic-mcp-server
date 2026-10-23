# metaheuristic-mcp-server

A stdio MCP server, launched by Claude Desktop, that gives a chat executor pwsh commands and exact file reading,
writing and editing through sessions identified by a `sessionId`. Every command runs in its own `pwsh` process under
a detached supervisor that writes the output and the exit code to files under `%MH_MCP_SERVER_HOME%`, so any number
of chats can use one server at once, a command that outlives its call continues as a job, and the server holds no
state in memory that a call depends on. Every request and response is recorded in a UTF-8 transcript per session.

## Release

A release is the git tag `v<version>` of this clone (equal to `__version__` of that commit), extracted once into its
own directory `C:\_mh\mh-mcp-server\dist\<version>\` - never inside this clone. Claude Desktop's config points
`PYTHONPATH` at the junction `dist\prod`, which points at the current `<version>\src`; a new release is a new
directory, the junction repointed and a Desktop restart; rollback is repointing it back. A release directory is never
edited, re-extracted or checked out in place: `supervisor.py` is started by path on every call, so a server running
from the clone, or a new version checked out under a running server, would mix versions or break every command at
once. Repointing `prod` under a running server is safe: the server resolves its own path through the junction at
import (`Path(__file__).resolve()`), so it keeps starting the `supervisor.py` of the version it loaded until it
exits; `getSessionId`'s `serverRoot` names that real directory.

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

Claude Desktop, `%APPDATA%\Claude\claude_desktop_config.json` - set once, never changed by a release:

```
"mh-mcp": {
  "command": "C:\\anaconda3\\envs\\python_3-13\\python.exe",
  "args": ["-m", "mh_mcp_server"],
  "env": { "PYTHONPATH": "C:\\_mh\\mh-mcp-server\\dist\\prod" }
}
```

`MH_MCP_SERVER_HOME` (an OS environment variable, `C:\_mh\mh-mcp-server`) is the runtime directory: `config.toml`,
`sessions/`, `state/`, `runs/`, `logs/server.log`, and the releases in `dist\<version>\`. Clearing state means
`sessions/`, `state/` and `runs/` - never `dist\`, which holds the running release. Without the variable the server
writes one line to stderr and exits 2.

## Configuration

`%MH_MCP_SERVER_HOME%\config.toml` over the built-in defaults (`config.DEFAULT_CONFIG_TOML`); an unknown table or key
is an error. `[excluded] paths` lists starts of absolute paths the file tools (`readFile`, `writeFile`, `editFile`)
refuse with `PATH_EXCLUDED`, even inside `[paths]` roots - a plain string prefix, case-insensitive, `/` and `\` alike,
so `C:\Windows` also covers `C:\WindowsApps` and `C:\Windows\` covers that directory only:

```
[excluded]
paths = ["C:\\Windows"]
```

## Tests

`cd C:\sandbox\_work\metaheuristic-mcp-server; python -m pytest` - real files, real `pwsh`, real processes, no
mocks; `--basetemp` is `C:/sandbox/_work/temp/mh-mcp-pytest` (`pyproject.toml`). The stdio tests start the server
from this clone, never Desktop.
