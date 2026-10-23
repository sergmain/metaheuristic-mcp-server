# metaheuristic-mcp-server

A stdio MCP server, launched by Claude Desktop, that gives a chat executor pwsh commands and exact file reading,
writing and editing through sessions identified by a `sessionId`. Every command runs in its own `pwsh` process under
a detached supervisor that writes the output and the exit code to files under `%MH_MCP_SERVER_HOME%`, so any number
of chats can use one server at once, a command that outlives its call continues as a job, and the server holds no
state in memory that a call depends on. Every request and response is recorded in a UTF-8 transcript per session.
