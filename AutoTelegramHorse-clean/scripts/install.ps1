$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$pythonCommand = Get-Command py -ErrorAction SilentlyContinue
if ($pythonCommand) {
    $python = $pythonCommand.Source
    $pythonArgs = @("-3")
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Python 3.11-3.13 не найден. Установите Python и добавьте его в PATH."
    }
    $python = $pythonCommand.Source
    $pythonArgs = @()
}

& $python @pythonArgs -m venv .venv
$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt
Write-Host "Установка завершена. Скопируйте .env.example в .env и заполните API_ID/API_HASH."
