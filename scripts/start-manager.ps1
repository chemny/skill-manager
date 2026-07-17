param(
  [string]$HostName = "127.0.0.1",
  [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Manager = Join-Path $ScriptRoot "skill-manager.ps1"

Write-Host "Starting Agent Skill Manager at http://$HostName`:$Port/"
& powershell -ExecutionPolicy Bypass -File $Manager -Action Web -HostName $HostName -Port $Port -Open
