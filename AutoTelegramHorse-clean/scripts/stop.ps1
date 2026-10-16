$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$pidFile = Join-Path $ProjectRoot "data\app.pid"
if (-not (Test-Path $pidFile)) { Write-Host "Файл PID не найден: приложение не запущено."; exit 0 }
$appPid = [int](Get-Content $pidFile -Raw).Trim()
try {
    Get-Process -Id $appPid -ErrorAction Stop | Out-Null
    Stop-Process -Id $appPid
    Write-Host "Приложение остановлено (PID $appPid)."
} catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
    Write-Host "Процесс с PID $appPid уже завершён."
}
Remove-Item $pidFile -Force
