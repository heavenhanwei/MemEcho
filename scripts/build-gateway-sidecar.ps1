#Requires -Version 5.1
<#
.SYNOPSIS
    Freeze the Python Gateway into the Tauri sidecar executable.

.DESCRIPTION
    Builds a self-contained executable with PyInstaller and copies it to the
    target-triple name expected by Tauri. No .env file, credential, media, or
    user data is added to the bundle.
#>

[CmdletBinding()]
param(
    [string]$TargetTriple = "x86_64-pc-windows-msvc"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$GatewayDir = Join-Path $RepoRoot "services\gateway"
$GatewaySource = Join-Path $GatewayDir "src"
$EntryPoint = Join-Path $GatewaySource "memecho_gateway\__main__.py"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$BuildRoot = Join-Path $RepoRoot ".runtime\gateway-sidecar-build"
$DistDir = Join-Path $BuildRoot "dist"
$WorkDir = Join-Path $BuildRoot "work"
$SpecDir = Join-Path $BuildRoot "spec"
$OutDir = Join-Path $RepoRoot "apps\desktop\src-tauri\binaries"
$TargetName = "memecho-gateway-$TargetTriple.exe"
$TargetPath = Join-Path $OutDir $TargetName

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Gateway sidecar build requires the repository virtualenv: $Python"
}
if (-not (Test-Path -LiteralPath $EntryPoint -PathType Leaf)) {
    throw "Gateway sidecar entry point is missing: $EntryPoint"
}
if ($TargetTriple -notmatch '^[A-Za-z0-9_.-]+$') {
    throw "TargetTriple contains unsupported characters"
}

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is missing. Install the gateway packaging extra: .\.venv\Scripts\python.exe -m pip install -e '.\services\gateway[packaging]'"
}

New-Item -ItemType Directory -Force -Path $DistDir, $WorkDir, $SpecDir, $OutDir | Out-Null

$Arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--name", "memecho-gateway",
    "--hide-console", "hide-early",
    "--paths", $GatewaySource,
    "--distpath", $DistDir,
    "--workpath", $WorkDir,
    "--specpath", $SpecDir,
    "--collect-submodules", "memecho_gateway",
    "--collect-submodules", "uvicorn",
    "--collect-submodules", "websockets",
    $EntryPoint
)

Write-Host "Freezing memEcho Gateway for $TargetTriple"
& $Python @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$FrozenExe = Join-Path $DistDir "memecho-gateway.exe"
if (-not (Test-Path -LiteralPath $FrozenExe -PathType Leaf)) {
    throw "PyInstaller completed without producing $FrozenExe"
}

Copy-Item -LiteralPath $FrozenExe -Destination $TargetPath -Force
$Hash = (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "Sidecar ready: $TargetPath"
Write-Host "SHA256: $Hash"
