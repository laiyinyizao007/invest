# Launch the invest VeighNa GUI against SimNow (paper trading) WITHOUT touching .env.
# Credentials are read at run time from <repo>\.env.simnow.bak (git-ignored) and injected into this process only.
# Nothing secret is stored in this script.
#
#   -Front second   (default) SimNow 7x24 front 40001/40011, usable any time, does not collide with drills on the first front
#   -Front first    SimNow first front 30001/30011 (trading hours only; do NOT use while scripts\drill.py is running)
#   -Check          only validate config and dependencies, do not open the window
param(
    [ValidateSet("first", "second")][string]$Front = "second",
    [switch]$Check
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env.simnow.bak"
$python = Join-Path $root ".venv\Scripts\python.exe"

function Stop-WithMessage([string]$message, [int]$code) {
    Write-Host $message -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit $code
}

if (-not (Test-Path $envFile)) { Stop-WithMessage "Missing $envFile" 2 }
if (-not (Test-Path $python)) { Stop-WithMessage "Missing $python (create the project .venv first)" 2 }

foreach ($line in Get-Content -Encoding UTF8 $envFile) {
    if ($line -match '^\s*#') { continue }
    if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { continue }
    $key = $Matches[1]
    $value = $Matches[2].Trim().Trim('"').Trim("'")
    if ($key -match '^(CTP_|LIVE_VT_SYMBOL$|HISTORY_VT_SYMBOL$)') { Set-Item -Path "Env:$key" -Value $value }
}

$ports = @{ first = @(30001, 30011); second = @(40001, 40011) }[$Front]
$env:CTP_TD_ADDRESS = $env:CTP_TD_ADDRESS -replace ':\d+\s*$', (':' + $ports[0])
$env:CTP_MD_ADDRESS = $env:CTP_MD_ADDRESS -replace ':\d+\s*$', (':' + $ports[1])

Write-Host "invest SimNow GUI - front: $Front (TD port $($ports[0]) / MD port $($ports[1]))" -ForegroundColor Cyan
Set-Location $root
$arguments = @("scripts\run_gui.py")
if ($Check) { $arguments += "--check" }
& $python @arguments
$exit = $LASTEXITCODE
if ($exit -ne 0) { Stop-WithMessage "run_gui.py exited with code $exit" $exit }
