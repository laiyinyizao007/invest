# Start DSH for invest research in a DEDICATED DSH_HOME (Issue #57).
#
# Why: dsh-agent-preset cannot opt out of global plugins. In a profile with global tool plugins
# (terminal, desktop automation, sub-agents, other MCP servers ...) the research session's model sees
# all of them (130+ tools observed on 2026-11-19), and "only a human can approve" no longer holds.
#
# What this script does, every launch:
#   1. Rebuilds the profile layer from DSH's shipped web template (--from-default-profile web),
#      so changes made through the UI do not accumulate.
#   2. Composes the profile with the two repo patches and runs tools/research_mcp/profile_check.py
#      (allowlist). Any finding => the script refuses to start.
#   3. Exports ONLY the model key (from a local, untracked env file). Nothing else from that file.
#   4. Boots the DSH web UI on that profile.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\research_mcp\start-research-dsh.ps1
#   ... -CheckOnly        compose + check, do not start
# The script is ASCII-only on purpose (Windows PowerShell 5.1 reads BOM-less files as ANSI).
param(
  [string]$DshCli = 'C:\Dev\projects\dsh-desktop\node_modules\.bin\dsh.cmd',
  [string]$DshHome = (Join-Path $env:USERPROFILE '.dsh-research'),
  [string]$EnvFile = 'C:\Dev\tools\dsh-dev.env',
  [string]$DesktopCredentials = (Join-Path $env:APPDATA 'dsh-desktop-dev\harness\.credentials.yaml'),
  [string]$Python = 'python',
  [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
# Fixed on purpose: research-mcp refuses to serve unless DSH_HOME holds exactly this one profile.
$Profile = 'research'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$presetPatch = Join-Path $PSScriptRoot 'dsh-research-preset.patch.yml'
$profilePatch = Join-Path $PSScriptRoot 'dsh-research-profile.patch.yml'
$checker = Join-Path $PSScriptRoot 'profile_check.py'
foreach ($p in @($DshCli, $presetPatch, $profilePatch, $checker)) {
  if (-not (Test-Path -LiteralPath $p)) { throw "missing: $p" }
}

# --from-default-profile REBUILDS the profile layer. Refuse any DSH_HOME that holds other profiles
# (e.g. the daily Desktop harness): pointing this script there would wipe that configuration.
$profilesDir = Join-Path $DshHome 'profiles'
if (Test-Path -LiteralPath $profilesDir) {
  $others = @(Get-ChildItem -LiteralPath $profilesDir -Directory | Where-Object { $_.Name -ne $Profile -and -not $_.Name.StartsWith('.') })
  if ($others.Count) {
    throw "DSH_HOME '$DshHome' also holds other profiles ($(($others | ForEach-Object Name) -join ', ')); use a dedicated home"
  }
}

$env:DSH_HOME = $DshHome
$env:INVEST_REPO = $repo
$composeArgs = @('--profile', $Profile, '--from-default-profile', 'web',
                 '--patch', $presetPatch, '--patch', $profilePatch)

# 1+2. compose and check (fail-closed: any error or finding stops here)
$dump = Join-Path ([System.IO.Path]::GetTempPath()) ("dsh-research-dump-" + [guid]::NewGuid().ToString('N') + '.yml')
try {
  & $DshCli @composeArgs --dump-config | Out-File -LiteralPath $dump -Encoding utf8
  if ($LASTEXITCODE -ne 0) { throw "dsh --dump-config failed (exit $LASTEXITCODE)" }
  & $Python $checker $dump
  if ($LASTEXITCODE -ne 0) { throw "profile_check refused this profile (exit $LASTEXITCODE); not starting" }
} finally {
  Remove-Item -LiteralPath $dump -ErrorAction SilentlyContinue
}
if ($CheckOnly) { Write-Host 'profile check passed (CheckOnly: not starting)'; exit 0 }

# 3. model key only. The reference name follows the provider id in dsh-research-profile.patch.yml.
#    Sources, in order:
#      a) the process environment;
#      b) the key the daily DSH Desktop already has (refs.<name> in its .credentials.yaml) -- ONLY that
#         one entry is read; grants/tokens in the same file are never touched;
#      c) an env file with DSH_PROVIDER_ID=minimax-cn + MODEL_API_KEY;
#      d) otherwise the key set once in this profile's Models settings (kept under the dedicated DSH_HOME).
#    Nothing else is exported; values are never printed.
$keyName = 'MINIMAX_CN_API_KEY'
function Read-CredentialRef([string]$path, [string]$name) {
  if (-not $path -or -not (Test-Path -LiteralPath $path)) { return $null }
  $inRefs = $false
  foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
    if ($line -match '^refs:\s*$') { $inRefs = $true; continue }
    if ($inRefs -and $line -match '^\S') { $inRefs = $false }
    if ($inRefs -and $line -match ('^\s+' + [regex]::Escape($name) + ':\s*(.+?)\s*$')) {
      return $Matches[1].Trim().Trim('"').Trim("'")
    }
  }
  return $null
}
if (-not [Environment]::GetEnvironmentVariable($keyName, 'Process')) {
  $value = Read-CredentialRef $DesktopCredentials $keyName
  $source = 'DSH Desktop model settings'
  if (-not $value -and $EnvFile -and (Test-Path -LiteralPath $EnvFile)) {
    $cfg = @{}
    foreach ($line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
      if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim() }
    }
    if ($cfg['DSH_PROVIDER_ID'] -eq 'minimax-cn' -and $cfg['MODEL_API_KEY']) { $value = $cfg['MODEL_API_KEY']; $source = 'env file' }
  }
  if ($value) {
    [Environment]::SetEnvironmentVariable($keyName, $value, 'Process')
    Write-Host "Exported $keyName from $source (model key only)"
  } else {
    Write-Host "No $keyName found: set the model key once in this profile's Models settings (kept in $DshHome)."
  }
  Remove-Variable value -ErrorAction SilentlyContinue
}

# 4. boot
Write-Host "Starting DSH research profile (DSH_HOME=$DshHome)"
& $DshCli @composeArgs
exit $LASTEXITCODE
