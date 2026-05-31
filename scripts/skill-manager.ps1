param(
  [ValidateSet("List", "Check", "Delete", "Scan", "Web", "Health", "Usage", "Report")]
  [string]$Action = "List",

  [string]$Name,
  [string]$Platform
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonScript = Join-Path $ScriptRoot "agent_skill_manager.py"

$ArgsList = @()
switch ($Action) {
  "List" { $ArgsList += "list" }
  "Check" { $ArgsList += "check-updates" }
  "Scan" { $ArgsList += "scan" }
  "Web" { $ArgsList += "web" }
  "Health" { $ArgsList += "health" }
  "Usage" { $ArgsList += "usage" }
  "Report" { $ArgsList += "report" }
  "Delete" {
    if ([string]::IsNullOrWhiteSpace($Name)) {
      throw "Delete is not destructive in this compatibility wrapper. Pass -Name to deactivate the capability, or use the Python CLI delete command for reviewed file removal."
    }
    $ArgsList += "deactivate"
    $ArgsList += $Name
  }
}

if (-not [string]::IsNullOrWhiteSpace($Platform)) {
  $ArgsList += "--platform"
  $ArgsList += $Platform
}

python $PythonScript @ArgsList
