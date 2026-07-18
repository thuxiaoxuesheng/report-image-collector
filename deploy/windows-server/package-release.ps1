param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$StagingRoot = Join-Path $ProjectRoot ".tmp\release-staging"
$DefaultOutput = Join-Path $ProjectRoot "dist\xhs-collector-release.zip"
$OutputPath = if ($OutputPath) {
    if ([IO.Path]::IsPathRooted($OutputPath)) {
        [IO.Path]::GetFullPath($OutputPath)
    } else {
        [IO.Path]::GetFullPath((Join-Path $ProjectRoot $OutputPath))
    }
} else {
    $DefaultOutput
}

function Assert-ProjectPath([string]$Path) {
    $FullPath = [IO.Path]::GetFullPath($Path)
    $Prefix = $ProjectRoot.TrimEnd('\') + '\'
    if (-not $FullPath.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝操作项目目录以外的路径：$FullPath"
    }
}

function Reset-Staging {
    Assert-ProjectPath $StagingRoot
    if (Test-Path -LiteralPath $StagingRoot) {
        Remove-Item -LiteralPath $StagingRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $StagingRoot -Force | Out-Null
}

function Copy-ReleaseTree([string]$RelativeSource) {
    $Source = Join-Path $ProjectRoot $RelativeSource
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "发布目录不存在：$Source"
    }
    $ExcludedDirectories = @(
        ".git", ".wxt", "__pycache__", "node_modules", "output", "packages"
    )
    foreach ($File in Get-ChildItem -LiteralPath $Source -Recurse -File) {
        $Relative = $File.FullName.Substring($ProjectRoot.Length).TrimStart('\', '/')
        $Segments = $Relative -split '[\\/]'
        if ($Segments | Where-Object { $_ -in $ExcludedDirectories }) { continue }
        if ($File.Name -eq ".env") { continue }
        if ($File.Extension -in @(".pyc", ".log")) { continue }
        $Destination = Join-Path $StagingRoot $Relative
        New-Item -ItemType Directory -Path (Split-Path $Destination) -Force | Out-Null
        Copy-Item -LiteralPath $File.FullName -Destination $Destination -Force
    }
}

Reset-Staging
try {
    foreach ($Directory in @("backend", "deploy", "docs", "scripts", "third_party")) {
        Copy-ReleaseTree $Directory
    }
    foreach ($FileName in @(
        ".env.example",
        ".gitignore",
        "LICENSE",
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "README.md",
        "setup.ps1",
        "start.ps1",
        "THIRD_PARTY_NOTICES.md"
    )) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $FileName) `
            -Destination (Join-Path $StagingRoot $FileName) -Force
    }

    $OutputDirectory = Split-Path $OutputPath
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    if (Test-Path -LiteralPath $OutputPath) {
        Remove-Item -LiteralPath $OutputPath -Force
    }
    Compress-Archive -Path (Join-Path $StagingRoot "*") `
        -DestinationPath $OutputPath -CompressionLevel Optimal
    $Hash = Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256
    # Keep these machine-readable labels ASCII so Windows PowerShell 5.1 also
    # renders them correctly when this UTF-8 script has no BOM.
    Write-Host "RELEASE_PATH=$OutputPath" -ForegroundColor Green
    Write-Host "SHA256=$($Hash.Hash)" -ForegroundColor Green
}
finally {
    Assert-ProjectPath $StagingRoot
    if (Test-Path -LiteralPath $StagingRoot) {
        Remove-Item -LiteralPath $StagingRoot -Recurse -Force
    }
}
