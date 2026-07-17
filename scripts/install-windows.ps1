param(
  [string]$SkillRoot = "$env:USERPROFILE\.agents\skills"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

function Test-Python {
  $commands = @(
    @{ Command = "py"; Args = @("-3") },
    @{ Command = "python"; Args = @() },
    @{ Command = "python3"; Args = @() }
  )
  foreach ($candidate in $commands) {
    if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) { continue }
    & $candidate.Command @($candidate.Args + @("-c", "import sys, sqlite3, http.server; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)")) 2>$null
    if ($LASTEXITCODE -eq 0) {
      return "$($candidate.Command) $($candidate.Args -join ' ')".Trim()
    }
  }
  return ""
}

$python = Test-Python
if (-not $python) {
  throw "Python 3.9+ was not found. Install Python from https://www.python.org/downloads/windows/ and enable 'Add python.exe to PATH'."
}

New-Item -ItemType Directory -Force -Path $SkillRoot | Out-Null

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptRoot
$ExpectedRoot = Join-Path $SkillRoot "skill-manager"

Write-Host "Python: $python"
Write-Host "Skill root: $SkillRoot"
Write-Host "Current folder: $ProjectRoot"
if ($ProjectRoot -ne $ExpectedRoot) {
  Write-Host ""
  Write-Host "Tip: for standard local-skill layout, keep this folder at:"
  Write-Host "  $ExpectedRoot"
}
Write-Host ""
Write-Host "Start the dashboard:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$ScriptRoot\start-manager.ps1`""
