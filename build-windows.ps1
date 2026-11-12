$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$env:UV_CACHE_DIR = Join-Path $PSScriptRoot '.uv-cache'
$buildPython = Join-Path $PSScriptRoot '.venv-win-build\Scripts\python.exe'
$python312 = Join-Path $PSScriptRoot '.venv-check\Scripts\python.exe'

if (-not (Test-Path $buildPython)) {
    if (Test-Path $python312) {
        uv venv --python $python312 .venv-win-build
    } else {
        uv venv --python 3.12 .venv-win-build
    }
}

uv pip install --python $buildPython -r requirements-lock.txt 'pyinstaller>=6.11,<7'
& $buildPython -m PyInstaller --noconfirm --clean packaging\AlarmTrend.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Write-Host "Built: $(Join-Path $PSScriptRoot 'dist\AlarmTrend\AlarmTrend.exe')"
