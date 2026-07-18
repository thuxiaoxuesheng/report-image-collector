$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe -m playwright install chromium

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "未检测到 Node.js，请先安装 Node.js 20 或更高版本"
}
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    npm install --global pnpm
}

pnpm install --frozen-lockfile

Push-Location ".\third_party\social-media-copilot"
try {
    pnpm install --frozen-lockfile
    pnpm build
    Push-Location ".\server"
    try {
        pnpm install --frozen-lockfile
    }
    finally {
        Pop-Location
    }
}
finally {
    Pop-Location
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
}

Write-Host "安装完成。本机开发可直接运行 .\start.ps1。公网或多用户使用请先按 README 启用站内认证。" -ForegroundColor Green
