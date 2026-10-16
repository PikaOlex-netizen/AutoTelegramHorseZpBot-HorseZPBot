$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { throw "Сначала выполните .\install.ps1" }
if (-not (Test-Path (Join-Path $ProjectRoot ".env"))) { throw "Создайте .env из .env.example" }

$dataDir = Join-Path $ProjectRoot "data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$pidFile = Join-Path $dataDir "app.pid"
if (Test-Path $pidFile) {
    $oldPid = [int](Get-Content $pidFile -Raw).Trim()
    try {
        $oldProcess = Get-Process -Id $oldPid -ErrorAction Stop
        Write-Host "Приложение уже запущено (PID $oldPid)."
        exit 0
    } catch {
        Remove-Item $pidFile -Force
    }
}

$appHost = (& $venvPython -c "from app.config import AppConfig; print(AppConfig.load().host)").Trim()
$appPort = [int](& $venvPython -c "from app.config import AppConfig; print(AppConfig.load().port)").Trim()
try {
    $listener = Get-NetTCPConnection -LocalPort $appPort -State Listen -ErrorAction Stop | Select-Object -First 1
    if ($listener) { throw "Порт $appPort уже используется (PID $($listener.OwningProcess)). Измените PORT в .env." }
} catch [System.Management.Automation.CommandNotFoundException] {
    # Get-NetTCPConnection is not available in some minimal Windows images.
}

$logFile = Join-Path $dataDir "server.log"
$errorLogFile = Join-Path $dataDir "server-error.log"
$process = Start-Process -FilePath $venvPython -ArgumentList @("-m", "app") -WorkingDirectory $ProjectRoot -RedirectStandardOutput $logFile -RedirectStandardError $errorLogFile -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 1
if ($process.HasExited) { throw "Приложение не запустилось. Проверьте data\server-error.log." }
$process.Id | Set-Content -Path $pidFile -NoNewline
Write-Host "Запущено: http://${appHost}:${appPort} (PID $($process.Id))"
