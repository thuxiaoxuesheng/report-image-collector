param(
    [switch]$NonInteractive,
    [string]$SiteUser = "collectoradmin",
    [string]$Domain = "",
    [string]$InternalEmail = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProgressPreference = "SilentlyContinue"

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "请右键 '安装服务器.cmd'，选择 '以管理员身份运行'。"
    }
}

function Assert-ValidSignature([string]$Path) {
    $signature = Get-AuthenticodeSignature -FilePath $Path
    if ($signature.Status -ne [Management.Automation.SignatureStatus]::Valid) {
        throw "安装包签名验证失败：$Path（$($signature.Status)）"
    }
    Write-Host "签名有效：$([IO.Path]::GetFileName($Path)) / $($signature.SignerCertificate.Subject)" -ForegroundColor DarkGray
}

function Invoke-Installer([string]$FilePath, [string]$Arguments) {
    Write-Host "正在安装 $([IO.Path]::GetFileName($FilePath)) ..." -ForegroundColor Cyan
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -Wait -PassThru
    if ($process.ExitCode -notin @(0, 1641, 3010)) {
        throw "安装失败：$FilePath，退出码 $($process.ExitCode)"
    }
}

function Get-OfficialPackage([string]$Url, [string]$Destination) {
    if (Test-Path $Destination) { return }
    Write-Host "正在从官方地址下载 $([IO.Path]::GetFileName($Destination)) ..." -ForegroundColor Cyan
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination
}

function New-UrlSafeSecret([int]$ByteCount = 32) {
    $Bytes = New-Object byte[] $ByteCount
    $Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Generator.GetBytes($Bytes) }
    finally { $Generator.Dispose() }
    return [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Set-EnvironmentValueIfMissing(
    [string]$Path,
    [string]$Name,
    [string]$Value,
    [Text.Encoding]$Encoding
) {
    $Content = if (Test-Path -LiteralPath $Path) {
        [IO.File]::ReadAllText($Path, $Encoding)
    } else { "" }
    $Pattern = "(?m)^$([Regex]::Escape($Name))=(.*)$"
    $Match = [Regex]::Match($Content, $Pattern)
    if ($Match.Success -and -not [string]::IsNullOrWhiteSpace($Match.Groups[1].Value)) {
        return
    }
    if ($Match.Success) {
        $Content = [Regex]::Replace(
            $Content,
            $Pattern,
            [Text.RegularExpressions.MatchEvaluator]{ param($Current) "$Name=$Value" },
            1
        )
        [IO.File]::WriteAllText($Path, $Content, $Encoding)
        return
    }
    $Prefix = if ($Content -and -not $Content.EndsWith("`n")) { "`r`n" } else { "" }
    [IO.File]::AppendAllText($Path, "$Prefix$Name=$Value`r`n", $Encoding)
}

function Set-EnvironmentValue(
    [string]$Path,
    [string]$Name,
    [string]$Value,
    [Text.Encoding]$Encoding
) {
    $Content = [IO.File]::ReadAllText($Path, $Encoding)
    $Pattern = "(?m)^$([Regex]::Escape($Name))=.*$"
    if ([Regex]::IsMatch($Content, $Pattern)) {
        $Content = [Regex]::Replace(
            $Content,
            $Pattern,
            [Text.RegularExpressions.MatchEvaluator]{ param($Current) "$Name=$Value" },
            1
        )
        [IO.File]::WriteAllText($Path, $Content, $Encoding)
        return
    }
    $Prefix = if ($Content -and -not $Content.EndsWith("`n")) { "`r`n" } else { "" }
    [IO.File]::AppendAllText($Path, "$Prefix$Name=$Value`r`n", $Encoding)
}

function Assert-NativeSuccess([string]$Operation) {
    if ($LASTEXITCODE -ne 0) { throw "$Operation 失败，退出码 $LASTEXITCODE" }
}

Assert-Administrator

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$PackageRoot = Join-Path $ProjectRoot "deploy\windows-server\packages"
$ChromeMsi = Join-Path $PackageRoot "googlechromestandaloneenterprise64.msi"
$NodeMsi = Join-Path $PackageRoot "node-v22.23.1-x64.msi"
$PythonInstaller = Join-Path $PackageRoot "python-3.13.14-amd64.exe"
$CaddyPackage = Join-Path $PackageRoot "caddy.exe"
$CaddyRoot = "C:\Caddy"
$CaddyExe = Join-Path $CaddyRoot "caddy.exe"
$Caddyfile = Join-Path $CaddyRoot "Caddyfile"
$CaddySitesRoot = Join-Path $CaddyRoot "sites"
$XhsCaddyfile = Join-Path $CaddySitesRoot "xhs-collector.caddy"
$EnvPath = Join-Path $ProjectRoot ".env"
$FreshConfiguration = -not (Test-Path -LiteralPath $EnvPath)
$CreatedCredentials = $false
$WasCollectorRunning = $false
$InteractiveWindowsIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$InteractiveWindowsUser = $InteractiveWindowsIdentity.Name
$InteractiveWindowsSid = $InteractiveWindowsIdentity.User.Value

if ([string]::IsNullOrWhiteSpace($Domain)) {
    if ($NonInteractive) { throw "非交互安装必须通过 -Domain 指定网站域名" }
    $Domain = (Read-Host "网站域名（例如 collector.example.com）").Trim().ToLowerInvariant()
}
if ($Domain -notmatch '^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$') {
    throw "域名格式无效：$Domain"
}
if ([string]::IsNullOrWhiteSpace($InternalEmail)) {
    $InternalEmail = "admin@$Domain"
}
if ($InternalEmail -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') {
    throw "管理员邮箱格式无效：$InternalEmail"
}

New-Item -ItemType Directory -Path $PackageRoot -Force | Out-Null
Get-OfficialPackage "https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi" $ChromeMsi
Get-OfficialPackage "https://nodejs.org/dist/v22.23.1/node-v22.23.1-x64.msi" $NodeMsi
Get-OfficialPackage "https://www.python.org/ftp/python/3.13.14/python-3.13.14-amd64.exe" $PythonInstaller
Get-OfficialPackage "https://caddyserver.com/api/download?os=windows&arch=amd64" $CaddyPackage

foreach ($required in @($ChromeMsi, $NodeMsi, $PythonInstaller, $CaddyPackage)) {
    if (-not (Test-Path $required)) {
        throw "部署包不完整，缺少：$required"
    }
}

New-Item -ItemType Directory -Path (Join-Path $ProjectRoot "logs") -Force | Out-Null
$Transcript = Join-Path $ProjectRoot "logs\install-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
Start-Transcript -Path $Transcript | Out-Null

try {
    $ExistingCollectorTask = Get-ScheduledTask -TaskName "XHS Collector" -ErrorAction SilentlyContinue
    if ($ExistingCollectorTask -and $ExistingCollectorTask.State -eq "Running") {
        $WasCollectorRunning = $true
        Stop-ScheduledTask -TaskName "XHS Collector"
        Start-Sleep -Seconds 2
    }
    Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object {
            Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
        }

    if (Test-Path -LiteralPath $EnvPath) {
        $BackupRoot = Join-Path $ProjectRoot "data\backups\upgrade-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
        Copy-Item -LiteralPath $EnvPath -Destination (Join-Path $BackupRoot ".env") -Force
        $ExistingDatabase = Join-Path $ProjectRoot "data\app.db"
        $ExistingPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
        if ((Test-Path -LiteralPath $ExistingDatabase) -and (Test-Path -LiteralPath $ExistingPython)) {
            & $ExistingPython -c `
                "import sqlite3,sys; src=sqlite3.connect(sys.argv[1]); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()" `
                $ExistingDatabase `
                (Join-Path $BackupRoot "app.db")
            if ($LASTEXITCODE -ne 0) { throw "升级前数据库备份失败" }
        }
        Write-Host "升级前配置和数据库已备份到：$BackupRoot" -ForegroundColor Green
    }

    Assert-ValidSignature $ChromeMsi
    Assert-ValidSignature $NodeMsi
    Assert-ValidSignature $PythonInstaller

    Invoke-Installer "msiexec.exe" "/i `"$ChromeMsi`" /qn /norestart"
    Invoke-Installer "msiexec.exe" "/i `"$NodeMsi`" /qn /norestart"
    Invoke-Installer $PythonInstaller "/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_test=0"

    New-Item -ItemType Directory -Path $CaddyRoot -Force | Out-Null
    $ReplaceCaddy = -not (Test-Path -LiteralPath $CaddyExe)
    if (-not $ReplaceCaddy) {
        $ReplaceCaddy = (Get-FileHash -LiteralPath $CaddyPackage -Algorithm SHA256).Hash -ne `
            (Get-FileHash -LiteralPath $CaddyExe -Algorithm SHA256).Hash
    }
    if ($ReplaceCaddy) {
        $CaddyBeforeCopy = Get-Service -Name "Caddy" -ErrorAction SilentlyContinue
        $RestartCaddyAfterCopy = $CaddyBeforeCopy -and $CaddyBeforeCopy.Status -eq "Running"
        try {
            if ($RestartCaddyAfterCopy) { Stop-Service -Name "Caddy" -Force }
            Copy-Item -LiteralPath $CaddyPackage -Destination $CaddyExe -Force
        }
        finally {
            if ($RestartCaddyAfterCopy) {
                $CurrentCaddy = Get-Service -Name "Caddy" -ErrorAction SilentlyContinue
                if ($CurrentCaddy -and $CurrentCaddy.Status -ne "Running") {
                    Start-Service -Name "Caddy"
                }
            }
        }
    }

    $Python = "C:\Program Files\Python313\python.exe"
    $Npm = "C:\Program Files\nodejs\npm.cmd"
    if (-not (Test-Path $Python)) { throw "未找到 Python：$Python" }
    if (-not (Test-Path $Npm)) { throw "未找到 Node.js：$Npm" }
    $env:Path = "C:\Program Files\Python313;C:\Program Files\Python313\Scripts;C:\Program Files\nodejs;$env:APPDATA\npm;$env:Path"

    Set-Location $ProjectRoot
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        & $Python -m venv .venv
    }
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip
    Assert-NativeSuccess "升级 pip"
    & .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
    Assert-NativeSuccess "安装 Python 依赖"
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $ProjectRoot ".playwright-browsers"
    & .\.venv\Scripts\python.exe -m playwright install chromium
    Assert-NativeSuccess "安装 Playwright Chromium"

    & $Npm install --global pnpm
    Assert-NativeSuccess "安装 pnpm"
    $Pnpm = Join-Path $env:APPDATA "npm\pnpm.cmd"
    if (-not (Test-Path $Pnpm)) { throw "pnpm 安装失败" }
    & $Pnpm install --frozen-lockfile
    Assert-NativeSuccess "安装根目录 Node 依赖"

    Push-Location ".\third_party\social-media-copilot"
    try {
        & $Pnpm install --frozen-lockfile
        Assert-NativeSuccess "安装浏览器扩展依赖"
        & $Pnpm build
        Assert-NativeSuccess "构建浏览器扩展"
        Push-Location ".\server"
        try {
            & $Pnpm install --frozen-lockfile
            Assert-NativeSuccess "安装浏览器桥接服务依赖"
        }
        finally { Pop-Location }
    }
    finally { Pop-Location }

    if ($FreshConfiguration) {
        $Utf8NoBom = [Text.UTF8Encoding]::new($false)
        $InitialEnvironment = @"
ENVIRONMENT=production
DEV_AUTH_BYPASS=false
ADMIN_EMAIL=$InternalEmail
DATABASE_URL=sqlite:///./data/app.db
DATABASE_POOL_SIZE=20
DATABASE_MAX_OVERFLOW=40
CLOUDFLARE_TEAM_DOMAIN=
CLOUDFLARE_AUD=
MINIMAX_API_KEY=
MINIMAX_REGION=cn
MINIMAX_VISION_TIMEOUT_SECONDS=90
DATA_DIR=./data
BROWSER_HEADLESS=false
BROWSER_CHANNEL=chrome
BROWSER_VIEWPORT_WIDTH=1280
BROWSER_VIEWPORT_HEIGHT=800
SOCIAL_COPILOT_ENABLED=true
SOCIAL_COPILOT_DIR=./third_party/social-media-copilot
SOCIAL_COPILOT_URL=http://127.0.0.1:3000
SOCIAL_COPILOT_BRIDGE_SECRET=
IMAGE_RETENTION_HOURS=24
IMAGE_HARD_LIMIT_HOURS=48
EXPORT_RETENTION_HOURS=2
MIN_ACTION_DELAY_SECONDS=5
MAX_ACTION_DELAY_SECONDS=9
MIN_LONG_PAUSE_SECONDS=20
MAX_LONG_PAUSE_SECONDS=35
DAILY_NEW_NOTE_LIMIT=0
MAX_SCAN_PER_KEYWORD=0
MAX_SCROLL_ROUNDS_PER_KEYWORD=0
EMBEDDED_WORKERS=true
COLLECTION_WORKER_CONCURRENCY=0
AI_WORKER_CONCURRENCY=0
BROWSER_MAX_CONCURRENCY=0
WORKER_LEASE_SECONDS=180
WORKER_HEARTBEAT_SECONDS=30
BROWSER_LEASE_SECONDS=180
BROWSER_LEASE_HEARTBEAT_SECONDS=30
LOGIN_FAILURE_WINDOW_SECONDS=300
LOGIN_MAX_FAILURES=6
LOGIN_LOCKOUT_SECONDS=900
"@
        [IO.File]::WriteAllText($EnvPath, $InitialEnvironment, $Utf8NoBom)

    if ($SiteUser -notmatch '^[A-Za-z0-9._-]{3,32}$') {
        throw "网站用户名只能包含字母、数字、点、下划线和连字符，长度3至32位"
    }

    if ($NonInteractive) {
        $RandomBytes = New-Object byte[] 24
        $RandomGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
        $RandomGenerator.GetBytes($RandomBytes)
        $RandomGenerator.Dispose()
        $PlainPassword = [Convert]::ToBase64String($RandomBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    }
    else {
        Write-Host "`n请设置网站入口账号。密码不会写入日志或配置明文。" -ForegroundColor Yellow
        do {
            $SiteUser = Read-Host "网站用户名（建议 collectoradmin）"
        } until ($SiteUser -match '^[A-Za-z0-9._-]{3,32}$')
        $SecurePassword = Read-Host "网站密码（输入时不会显示）" -AsSecureString
        $Credential = [PSCredential]::new($SiteUser, $SecurePassword)
        $PlainPassword = $Credential.GetNetworkCredential().Password
        if ($PlainPassword.Length -lt 12) { throw "网站密码至少需要12位" }
    }

    $SitePasswordHash = (& .\.venv\Scripts\python.exe -c `
        "from backend.app.site_auth import hash_site_password; import sys; print(hash_site_password(sys.argv[1]))" `
        $PlainPassword).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $SitePasswordHash) {
        throw "网站密码哈希生成失败"
    }
    $SessionBytes = New-Object byte[] 32
    $SessionGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
    $SessionGenerator.GetBytes($SessionBytes)
    $SessionGenerator.Dispose()
    $SessionSecret = [Convert]::ToBase64String($SessionBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $MasterBytes = New-Object byte[] 32
    $MasterGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
    $MasterGenerator.GetBytes($MasterBytes)
    $MasterGenerator.Dispose()
    $MasterSecret = [Convert]::ToBase64String($MasterBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')

        $AuthenticationEnvironment = @"

SITE_AUTH_ENABLED=true
SITE_AUTH_USERNAME=$SiteUser
SITE_AUTH_PASSWORD_HASH=$SitePasswordHash
SITE_AUTH_SESSION_SECRET=$SessionSecret
SITE_AUTH_SESSION_DAYS=30
SECRETS_MASTER_KEY=$MasterSecret
"@
        [IO.File]::AppendAllText($EnvPath, $AuthenticationEnvironment, $Utf8NoBom)
        $CreatedCredentials = $true

    if ($NonInteractive) {
        $LoginInfoPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "网站登录信息.txt"
        @"
网站：https://$Domain
用户名：$SiteUser
密码：$PlainPassword

请保存到自己的密码管理器，然后删除本文件。
"@ | Set-Content -LiteralPath $LoginInfoPath -Encoding UTF8
        & icacls.exe $LoginInfoPath `
            /inheritance:r `
            /grant:r `
                "*S-1-5-32-544:F" `
                "*S-1-5-18:F" `
                "*$($InteractiveWindowsSid):F" | Out-Null
    }

    $PlainPassword = $null
    $SitePasswordHash = $null
    $SessionSecret = $null
    $MasterSecret = $null
    $Credential = $null
        $SecurePassword = $null
    }

    # Releases before multi-user model settings had no master key. Generate it
    # during an in-place upgrade, but never rotate a non-empty existing value:
    # rotating it would make users' encrypted API keys unreadable.
    $Utf8NoBom = [Text.UTF8Encoding]::new($false)
    Set-EnvironmentValueIfMissing `
        -Path $EnvPath `
        -Name "SECRETS_MASTER_KEY" `
        -Value (New-UrlSafeSecret) `
        -Encoding $Utf8NoBom
    foreach ($ProductionSetting in @(
        @{ Name = "ENVIRONMENT"; Value = "production" },
        @{ Name = "DEV_AUTH_BYPASS"; Value = "false" },
        @{ Name = "SITE_AUTH_ENABLED"; Value = "true" },
        @{ Name = "BROWSER_HEADLESS"; Value = "false" }
    )) {
        Set-EnvironmentValue `
            -Path $EnvPath `
            -Name $ProductionSetting.Name `
            -Value $ProductionSetting.Value `
            -Encoding $Utf8NoBom
    }

    $DataRoot = Join-Path $ProjectRoot "data"
    $LogRoot = Join-Path $ProjectRoot "logs"
    New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    foreach ($SensitiveDirectory in @($DataRoot, $LogRoot)) {
        # Secure the root first, then make every existing child inherit that
        # descriptor. Passing (OI)(CI) grants directly to ordinary files while
        # using /T can leave those files with an empty ACL on Windows.
        & takeown.exe /F $SensitiveDirectory /R /D Y | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "敏感目录所有权接管失败：$SensitiveDirectory" }
        & icacls.exe $SensitiveDirectory `
            /inheritance:r `
            /remove:g "*S-1-5-32-545" "*S-1-5-11" `
            /grant:r `
                "*S-1-5-32-544:(OI)(CI)F" `
                "*S-1-5-18:(OI)(CI)F" `
                "*$($InteractiveWindowsSid):(OI)(CI)F" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "敏感目录权限设置失败：$SensitiveDirectory" }
        $Children = Join-Path $SensitiveDirectory "*"
        & icacls.exe $Children /reset /T /C | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "敏感目录子项权限继承失败：$SensitiveDirectory" }
    }
    & icacls.exe $EnvPath `
        /inheritance:r `
        /remove:g "*S-1-5-32-545" "*S-1-5-11" `
        /grant:r `
            "*S-1-5-32-544:F" `
            "*S-1-5-18:F" `
            "*$($InteractiveWindowsSid):F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ".env 权限设置失败" }

    New-Item -ItemType Directory -Path $CaddySitesRoot -Force | Out-Null
    $ExistingCaddyContent = if (Test-Path -LiteralPath $Caddyfile) {
        Get-Content -LiteralPath $Caddyfile -Raw
    } else { "" }
    if ($ExistingCaddyContent -notmatch ([Regex]::Escape($Domain) + '\s*\{')) {
        @"
$Domain {
    encode zstd gzip

    request_header -Cf-Access-Jwt-Assertion
    request_header -Cf-Access-Authenticated-User-Email

    header {
        -Server
        X-Content-Type-Options nosniff
        Referrer-Policy no-referrer
        Permissions-Policy "camera=(), microphone=(), geolocation=()"
    }

    reverse_proxy 127.0.0.1:8765
}
"@ | Set-Content -LiteralPath $XhsCaddyfile -Encoding UTF8
        if (-not $ExistingCaddyContent) {
            "import sites/*.caddy" | Set-Content -LiteralPath $Caddyfile -Encoding UTF8
        }
        elseif ($ExistingCaddyContent -notmatch '(?m)^\s*import\s+sites/\*\.caddy\s*$') {
            Add-Content -LiteralPath $Caddyfile -Value "`r`nimport sites/*.caddy" -Encoding UTF8
        }
    }
    else {
        Write-Host "检测到现有 $Domain 配置，保持原 Caddy 站点块不变。" -ForegroundColor Yellow
    }

    & $CaddyExe validate --config $Caddyfile --adapter caddyfile

    $CaddyCommand = "`"$CaddyExe`" run --config `"$Caddyfile`" --adapter caddyfile"
    $ExistingCaddy = Get-Service -Name "Caddy" -ErrorAction SilentlyContinue
    if (-not $ExistingCaddy) {
        New-Service -Name "Caddy" -BinaryPathName $CaddyCommand -DisplayName "Caddy HTTPS Web Server" -StartupType Automatic | Out-Null
    }
    else {
        if ($ExistingCaddy.Status -eq "Running") { Stop-Service -Name "Caddy" -Force }
        Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\Caddy" -Name ImagePath -Value $CaddyCommand
        Set-Service -Name "Caddy" -StartupType Automatic
    }

    foreach ($rule in @(
        @{ Name = "XHS Collector HTTP"; Port = 80 },
        @{ Name = "XHS Collector HTTPS"; Port = 443 }
    )) {
        if (-not (Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Action Allow -Protocol TCP -LocalPort $rule.Port | Out-Null
        }
    }

    $TaskName = "XHS Collector"
    $TaskAction = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\start-server.ps1`"" `
        -WorkingDirectory $ProjectRoot
    # Headed Chromium is deliberately used to reduce headless-specific account
    # risk. It must live in a real desktop session, so start after this Windows
    # administrator signs in. Disconnecting RDP keeps that session and task alive.
    $TaskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $InteractiveWindowsUser
    $TaskPrincipal = New-ScheduledTaskPrincipal `
        -UserId $InteractiveWindowsUser `
        -LogonType Interactive `
        -RunLevel Limited
    $TaskSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $TaskAction `
        -Trigger $TaskTrigger `
        -Principal $TaskPrincipal `
        -Settings $TaskSettings `
        -Force | Out-Null

    & .\.venv\Scripts\python.exe -m pytest
    Assert-NativeSuccess "运行测试"
    & .\.venv\Scripts\python.exe -m ruff check backend
    Assert-NativeSuccess "运行代码检查"

    Start-Service -Name "Caddy"
    Start-ScheduledTask -TaskName $TaskName

    Write-Host "`n服务器安装完成。" -ForegroundColor Green
    Write-Host "网站地址：https://$Domain"
    if ($CreatedCredentials) {
        Write-Host "网站用户名：$SiteUser"
        if ($NonInteractive) { Write-Host "网站密码保存在服务器桌面的 '网站登录信息.txt'，请保存后删除。" }
    }
    else {
        Write-Host "检测到现有 .env，账号、主密钥和模型密钥均已保留。" -ForegroundColor Green
    }
    Write-Host "后端仅监听：127.0.0.1:8765"
    Write-Host "浏览器运行方式：$InteractiveWindowsUser 登录后自动启动（断开RDP可以，不能注销）。"
    Write-Host "下一步：为 $Domain 添加 A/AAAA 记录，并在云防火墙放行 TCP 80、443。" -ForegroundColor Yellow
}
finally {
    if ($WasCollectorRunning) {
        $CollectorTask = Get-ScheduledTask -TaskName "XHS Collector" -ErrorAction SilentlyContinue
        if ($CollectorTask -and $CollectorTask.State -ne "Running") {
            try { Start-ScheduledTask -TaskName "XHS Collector" }
            catch { Write-Warning "原网站任务未能自动恢复：$($_.Exception.Message)" }
        }
    }
    Stop-Transcript | Out-Null
}
