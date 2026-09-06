# ============================================================
# fix_terminal.ps1 - one-click VS Code terminal recovery
#
# What it does:
#   1. Kill zombie shell sessions (powershell/cmd) older than 24h
#      - these stale sessions cause: git not on PATH / swallowed
#        output / frozen commands
#   2. Verify key tools exist on PATH (git/docker/gh/bash)
#   3. Print the tail of the machine-level PATH for debugging
#
# NOTE: ASCII-only on purpose - PowerShell 5.1 mis-parses UTF-8
#       files without BOM under GBK locale.
#
# Usage (in PowerShell terminal):
#   powershell -ExecutionPolicy Bypass -File scripts\fix_terminal.ps1
# ============================================================

$ErrorActionPreference = 'SilentlyContinue'

Write-Host "=== 1. Clean zombie shell sessions (>24h old) ===" -ForegroundColor Cyan
$cutoff = (Get-Date).AddHours(-24)
$zombies = Get-Process powershell, pwsh, cmd -ErrorAction SilentlyContinue |
    Where-Object { $_.Id -ne $PID -and $_.StartTime -lt $cutoff }
if ($zombies) {
    foreach ($z in $zombies) {
        Write-Host ("   kill PID {0} ({1}, started {2})" -f $z.Id, $z.ProcessName, $z.StartTime)
        Stop-Process -Id $z.Id -Force
    }
} else {
    Write-Host "   no zombies to clean"
}

Write-Host ""
Write-Host "=== 2. Tool PATH check ===" -ForegroundColor Cyan
$tools = 'git', 'docker', 'gh', 'bash', 'python'
foreach ($t in $tools) {
    $g = Get-Command $t -ErrorAction SilentlyContinue
    if ($g) { Write-Host ("  [OK]   {0} -> {1}" -f $t, $g.Source) -ForegroundColor Green }
    else    { Write-Host ("  [MISS] {0} not on PATH!" -f $t) -ForegroundColor Red }
}

Write-Host ""
Write-Host "=== 3. Machine PATH tail (Docker/Git present?) ===" -ForegroundColor Cyan
[Environment]::GetEnvironmentVariable('Path', 'Machine') -split ';' |
    Where-Object { $_ -match 'Docker|Git|GitHub|cloudflared' } |
    ForEach-Object { Write-Host "  $_" }

Write-Host ""
Write-Host "=== 4. Advice ===" -ForegroundColor Cyan
Write-Host "  - If any [MISS] above: close ALL terminals, then reopen VS Code"
Write-Host "  - Still broken: Ctrl+Shift+P -> 'Terminal: Kill All Terminals', then new terminal"
Write-Host "  Done."

