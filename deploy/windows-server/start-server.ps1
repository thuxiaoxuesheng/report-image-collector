$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$LogDir = Join-Path $ProjectRoot "logs"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PlaywrightBrowsers = Join-Path $ProjectRoot ".playwright-browsers"

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
Set-Location $ProjectRoot

if (Test-Path $PlaywrightBrowsers) {
    $env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightBrowsers
}

if (-not (Test-Path $Python)) {
    Add-Content -Path (Join-Path $LogDir "startup-errors.log") -Value "$(Get-Date -Format s) 未找到虚拟环境：$Python"
    exit 1
}

$LogConfig = Join-Path $ProjectRoot "deploy\windows-server\logging.json"
$StartupLog = Join-Path $LogDir "startup-errors.log"

# Windows PowerShell 5.1 wraps a native process' stderr as NativeCommandError.
# Uvicorn writes normal lifecycle logs to stderr, so ErrorAction=Stop would
# otherwise terminate the wrapper immediately after a successful start.
$ErrorActionPreference = "Continue"
& $Python -m uvicorn backend.app.main:app `
    --host 127.0.0.1 `
    --port 8765 `
    --no-proxy-headers `
    --log-config $LogConfig *>> $StartupLog
exit $LASTEXITCODE
