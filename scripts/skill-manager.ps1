param(
  [ValidateSet("List", "Check", "Delete", "Scan", "Web", "Health", "Usage", "Report")]
  [string]$Action = "Web",

  [string]$Name,
  [string]$Platform,
  [string]$HostName = "127.0.0.1",
  [int]$Port = 8765,
  [switch]$Open
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

function Find-Python {
  $candidates = @(
    @{ Command = "py"; Args = @("-3") },
    @{ Command = "python"; Args = @() },
    @{ Command = "python3"; Args = @() }
  )
  foreach ($candidate in $candidates) {
    $cmd = Get-Command $candidate.Command -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
      $version = & $candidate.Command @($candidate.Args + @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)")) 2>$null
      if ($LASTEXITCODE -eq 0) {
        return $candidate
      }
    } catch {
      continue
    }
  }
  throw "Python 3.9+ was not found. Install Python from https://www.python.org/downloads/windows/ and enable 'Add python.exe to PATH', then run this script again."
}

function Add-OptionalPlatformArg {
  param([System.Collections.Generic.List[string]]$ArgsList)
  if (-not [string]::IsNullOrWhiteSpace($Platform)) {
    $ArgsList.Add("--platform")
    $ArgsList.Add($Platform)
  }
}

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonScript = Join-Path $ScriptRoot "agent_skill_manager.py"
$Python = Find-Python
$ArgsList = [System.Collections.Generic.List[string]]::new()

switch ($Action) {
  "List" {
    $ArgsList.Add("list")
    Add-OptionalPlatformArg $ArgsList
  }
  "Check" {
    $ArgsList.Add("check-updates")
    Add-OptionalPlatformArg $ArgsList
  }
  "Scan" {
    $ArgsList.Add("scan")
  }
  "Web" {
    $ArgsList.Add("web")
    $ArgsList.Add("--host")
    $ArgsList.Add($HostName)
    $ArgsList.Add("--port")
    $ArgsList.Add([string]$Port)
    if ($Open) {
      $ArgsList.Add("--open")
    }
  }
  "Health" {
    $ArgsList.Add("health")
  }
  "Usage" {
    $ArgsList.Add("usage")
  }
  "Report" {
    $ArgsList.Add("report")
  }
  "Delete" {
    if ([string]::IsNullOrWhiteSpace($Name)) {
      throw "Delete is intentionally not destructive in this compatibility wrapper. Pass -Name to deactivate the capability, or use the Python CLI delete command for reviewed file removal."
    }
    $ArgsList.Add("deactivate")
    $ArgsList.Add($Name)
    Add-OptionalPlatformArg $ArgsList
  }
}

& $Python.Command @($Python.Args + @($PythonScript) + $ArgsList)
