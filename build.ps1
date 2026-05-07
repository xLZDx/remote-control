<#
.SYNOPSIS
    Build the RemoteControl distributable.

.DESCRIPTION
    Single-command build:
        1. Validate Python venv (or create it)
        2. Install runtime + build dependencies
        3. Run pytest
        4. PyInstaller -> dist\RemoteControl\
        5. Inno Setup compile -> installer\Output\RemoteControlSetup.exe (if iscc found)

    Usage:
        .\build.ps1                 # full build
        .\build.ps1 -SkipTests      # skip pytest (CI fast path)
        .\build.ps1 -SkipInstaller  # PyInstaller only, no .exe installer
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition

Write-Host ""
Write-Host "=== RemoteControl build ===" -ForegroundColor Cyan
Write-Host "  project: $projectRoot"

# 1. venv
$venvPython = Join-Path $projectRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host ""
    Write-Host "Creating venv..." -ForegroundColor Yellow
    & python -m venv (Join-Path $projectRoot "venv")
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}

# 2. deps
Write-Host ""
Write-Host "Installing dependencies (no cache)..." -ForegroundColor Yellow
$env:PIP_NO_CACHE_DIR = "1"
& $venvPython -m pip install --upgrade pip setuptools wheel | Out-Null
& $venvPython -m pip install -r (Join-Path $projectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# 3. tests
if (-not $SkipTests) {
    Write-Host ""
    Write-Host "Running tests..." -ForegroundColor Yellow
    Push-Location $projectRoot
    try {
        & $venvPython -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw "tests failed" }
    } finally {
        Pop-Location
    }
}

# 4. PyInstaller
Write-Host ""
Write-Host "Building EXE with PyInstaller..." -ForegroundColor Yellow
Push-Location $projectRoot
try {
    & $venvPython -m PyInstaller --clean --noconfirm (Join-Path $projectRoot "installer\RemoteControl.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
} finally {
    Pop-Location
}
$distPath = Join-Path $projectRoot "dist\RemoteControl\RemoteControl.exe"
if (-not (Test-Path $distPath)) { throw "expected $distPath was not produced" }
$exeSize = (Get-Item $distPath).Length / 1MB
Write-Host ("  produced: $distPath  ({0:N1} MB)" -f $exeSize) -ForegroundColor Green

# 5. Inno Setup
if (-not $SkipInstaller) {
    Write-Host ""
    Write-Host "Compiling installer..." -ForegroundColor Yellow
    $iscc = $null
    foreach ($p in @(
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    )) {
        if (Test-Path $p) { $iscc = $p; break }
    }
    if ($null -eq $iscc) {
        Write-Host "  Inno Setup 6 not found - skipping installer (install from https://jrsoftware.org/isdl.php to enable)" -ForegroundColor Yellow
    } else {
        & $iscc (Join-Path $projectRoot "installer\RemoteControl.iss")
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup compile failed" }
        $setupExe = Join-Path $projectRoot "installer\Output\RemoteControlSetup.exe"
        if (Test-Path $setupExe) {
            $sz = (Get-Item $setupExe).Length / 1MB
            Write-Host ("  produced: $setupExe  ({0:N1} MB)" -f $sz) -ForegroundColor Green
        }
    }
}

Write-Host ""
Write-Host "Build complete." -ForegroundColor Cyan
