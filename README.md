# metaheuristic-mcp-server

A stdio MCP server, launched by Claude Desktop, that gives a chat executor pwsh commands and exact file reading,
writing and editing through sessions identified by a `sessionId`. Every command runs in its own `pwsh` process under
a detached supervisor that writes the output and the exit code to files under `%MH_MCP_SERVER_HOME%`, so any number
of chats can use one server at once, a command that outlives its call continues as a job, and the server holds no
state in memory that a call depends on. Every request and response is recorded in a UTF-8 transcript per session.

## Release

A release is the git tag `v<version>` of this clone (equal to `__version__` of that commit), extracted once into its
own directory `C:\_mh\mh-mcp-server\dist\<version>\` - never inside this clone. Claude Desktop's config points
`PYTHONPATH` at `<version>\src`; a new release is a new directory, that one string changed and a Desktop restart;
rollback is the previous string. A release directory is never edited, re-extracted or checked out in place:
`supervisor.py` is started by path on every call, so a server running from the clone, or a new version checked out
under a running server, would mix versions or break every command at once.

```
git -C C:\sandbox\_work\metaheuristic-mcp-server tag v0.1.0
git -C C:\sandbox\_work\metaheuristic-mcp-server push origin v0.1.0
New-Item -ItemType Directory -Force C:\_mh\mh-mcp-server\dist | Out-Null
git -C C:\sandbox\_work\metaheuristic-mcp-server archive --format=zip -o C:\_mh\mh-mcp-server\dist\v0.1.0.zip v0.1.0 src
Expand-Archive C:\_mh\mh-mcp-server\dist\v0.1.0.zip C:\_mh\mh-mcp-server\dist\0.1.0
```

`Expand-Archive` refuses an existing destination, which is the point: a release directory is created once.

Claude Desktop, `%APPDATA%\Claude\claude_desktop_config.json`:

```
"mh-mcp": {
  "command": "C:\\anaconda3\\envs\\python_3-13\\python.exe",
  "args": ["-m", "mh_mcp_server"],
  "env": { "PYTHONPATH": "C:\\_mh\\mh-mcp-server\\dist\\0.1.0\\src" }
}
```

`MH_MCP_SERVER_HOME` (an OS environment variable, `C:\_mh\mh-mcp-server`) is the runtime directory: `config.toml`,
`sessions/`, `state/`, `runs/`, `logs/server.log`, and the releases in `dist\<version>\`. Clearing state means
`sessions/`, `state/` and `runs/` - never `dist\`, which holds the running release. Without the variable the server
writes one line to stderr and exits 2.

## Tests

`cd C:\sandbox\_work\metaheuristic-mcp-server; python -m pytest` - real files, real `pwsh`, real processes, no
mocks; `--basetemp` is `C:/sandbox/_work/temp/mh-mcp-pytest` (`pyproject.toml`). The stdio tests start the server
from this clone, never Desktop.
