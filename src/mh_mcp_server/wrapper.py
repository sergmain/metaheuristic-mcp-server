"""The pwsh script a command runs as: the user's text wrapped for its exit code and session state (Phase 6).

The exit-code rule (decision 20): `exit N` -> N; a terminating error -> 1; the last statement failed -> its native
exit code, or 1 when it has none; otherwise 0. This is `pwsh -Command`'s rule with the native code kept, because
`pwsh -File` reports 0 for any script that ends without `exit`.

The epilogue, in `finally`, writes `state-out.json` - `{"cwd", "envSet", "envRemoved"}`, UTF-8 without BOM: the
working directory at the end and the process environment's difference against its start.
"""

from __future__ import annotations

from pathlib import Path

_PROLOGUE = """\
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding
$__mcpEnv = @{}; foreach ($e in [Environment]::GetEnvironmentVariables('Process').GetEnumerator()) { $__mcpEnv[$e.Key] = $e.Value }
$__mcpOk = $true; $__mcpTerminated = $false
try {
"""

_MIDDLE = """
$__mcpOk = $?
} catch {
    $__mcpTerminated = $true; $_ | Out-String | Write-Output
} finally {
    try {
        $__mcpNow = @{}; foreach ($e in [Environment]::GetEnvironmentVariables('Process').GetEnumerator()) { $__mcpNow[$e.Key] = $e.Value }
        $__mcpSet = [ordered]@{}
        foreach ($k in $__mcpNow.Keys) { if (-not $__mcpEnv.ContainsKey($k) -or $__mcpEnv[$k] -cne $__mcpNow[$k]) { $__mcpSet[$k] = $__mcpNow[$k] } }
        $__mcpRemoved = @(foreach ($k in $__mcpEnv.Keys) { if (-not $__mcpNow.ContainsKey($k)) { $k } })
        $__mcpState = [ordered]@{ cwd = $PWD.ProviderPath; envSet = $__mcpSet; envRemoved = $__mcpRemoved }
"""

_EPILOGUE = """\
    } catch { }
}
if ($__mcpTerminated) { exit 1 }
if (-not $__mcpOk) { if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 } }
exit 0
"""


def ps_literal(text: str) -> str:
    """A single-quoted PowerShell string literal of `text`."""
    return "'" + text.replace("'", "''") + "'"


def render_wrapper(user_command: str, state_out: Path) -> str:
    """The script text: prologue, the user command verbatim on its own lines, epilogue writing `state_out`."""
    write_state = ("        [System.IO.File]::WriteAllText(" + ps_literal(str(state_out))
                   + ", ($__mcpState | ConvertTo-Json -Depth 4 -Compress), [System.Text.UTF8Encoding]::new($false))\n")
    return _PROLOGUE + user_command + _MIDDLE + write_state + _EPILOGUE
