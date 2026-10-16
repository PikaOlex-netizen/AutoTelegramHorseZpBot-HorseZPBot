$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $ProjectRoot "scripts\stop.ps1")
Start-Sleep -Seconds 1
& (Join-Path $ProjectRoot "scripts\start.ps1")
