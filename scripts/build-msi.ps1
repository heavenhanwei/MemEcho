# build-msi.ps1 — Build memEcho MSI/NSIS installers for roadshow or local testing.
#
# Simplified wrapper around `pnpm tauri build`. For production releases with
# full quality gates and clean-worktree enforcement, use build-windows-release.ps1.
#
# Usage (GatewayUrl is required — the frontend production build refuses without it):
#   .\scripts\build-msi.ps1 -GatewayUrl https://gateway.example.com
#   .\scripts\build-msi.ps1 -GatewayUrl https://gateway.example.com -OutputDir C:\release
#   .\scripts\build-msi.ps1 -GatewayUrl https://gateway.example.com -SkipFrontend
#
# GatewayUrl must be a clean HTTPS origin (no path/query/credentials). For local
# acceptance use a non-sensitive placeholder like https://gateway.example.com and
# override the gateway in app settings at runtime.
#
# Prerequisites:
#   - Visual Studio 2022 Build Tools (C++ desktop dev + Windows SDK)
#   - Rust stable x86_64-pc-windows-msvc
#   - Node.js + pnpm
#   - WiX Toolset v3 (for MSI) and NSIS (for setup.exe)

param(
    [string]$GatewayUrl = "",
    [string]$OutputDirectory = "release-artifacts",
    [switch]$SkipFrontend,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

function Write-Step($msg) { Write-Host "`n>>> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  WARN: $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  FAIL: $msg" -ForegroundColor Red }

# pnpm/cargo emit normal logs on stderr; with ErrorActionPreference=Stop a 2>&1
# merge throws NativeCommandError. Lower preference while capturing; callers
# still decide success via $LASTEXITCODE.
function Invoke-NativeCapture([scriptblock]$Block) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Block } finally { $ErrorActionPreference = $prev }
}

# ─── Preflight ───────────────────────────────────────────────────────────────

Write-Step "Checking build prerequisites"

# Rust
$rustup = Get-Command rustup -ErrorAction SilentlyContinue
if ($rustup) {
    $rustVer = Invoke-NativeCapture { & rustc --version 2>&1 }
    Write-Ok "Rust: $rustVer"
} else {
    Write-Fail "Rust not found. Install from https://rustup.rs"
    exit 1
}

# pnpm
$pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
if ($pnpm) {
    $pnpmVer = Invoke-NativeCapture { & pnpm --version 2>&1 }
    Write-Ok "pnpm: v$pnpmVer"
} else {
    Write-Fail "pnpm not found."
    exit 1
}

# Tauri CLI
$tauriCli = Get-Command tauri -ErrorAction SilentlyContinue
if (-not $tauriCli) {
    # Try via pnpm
    $tauriCheck = Invoke-NativeCapture { & pnpm tauri --version 2>&1 }
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Tauri CLI: via pnpm"
    } else {
        Write-Fail "Tauri CLI not found. Install: pnpm add -D @tauri-apps/cli"
        exit 1
    }
} else {
    Write-Ok "Tauri CLI: installed"
}

# Git status (warning only)
$gitStatus = Invoke-NativeCapture { & git status --porcelain --untracked-files=no 2>&1 }
if ($gitStatus) {
    Write-Warn "Working tree has uncommitted changes (non-blocking for dev build)"
}

# ─── Gateway URL ─────────────────────────────────────────────────────────────

if (-not $GatewayUrl) {
    Write-Fail "GatewayUrl is required. Pass a clean HTTPS origin, e.g. -GatewayUrl https://gateway.example.com"
    exit 1
}

Write-Step "Baking gateway URL into build"
$parsed = $null
if (-not [Uri]::TryCreate($GatewayUrl, [UriKind]::Absolute, [ref]$parsed)) {
    Write-Fail "Invalid GatewayUrl: $GatewayUrl"
    exit 1
}
if ($parsed.Scheme -ne "https") {
    Write-Fail "GatewayUrl must use HTTPS (got '$($parsed.Scheme)'). HTTP and localhost origins are not accepted."
    exit 1
}
if ($parsed.UserInfo -or $parsed.PathAndQuery -ne "/" -or $parsed.Fragment) {
    Write-Fail "GatewayUrl must be a clean origin without path, query, fragment, or credentials: $GatewayUrl"
    exit 1
}
$env:VITE_GATEWAY_URL = $parsed.GetLeftPart([UriPartial]::Authority)
Write-Ok "VITE_GATEWAY_URL = $env:VITE_GATEWAY_URL"

# Ensure no token is embedded
Remove-Item Env:VITE_GATEWAY_TOKEN -ErrorAction SilentlyContinue

# ─── Version info ────────────────────────────────────────────────────────────

Write-Step "Reading version info"

$tauriConf = Get-Content (Join-Path $RepoRoot "apps\desktop\src-tauri\tauri.conf.json") -Raw | ConvertFrom-Json
$version = $tauriConf.version
$commit = Invoke-NativeCapture { & git rev-parse --short HEAD 2>&1 }
Write-Ok "Version: $version"
Write-Ok "Commit:  $commit"

# ─── Tests ───────────────────────────────────────────────────────────────────

if (-not $SkipTests) {
    Write-Step "Running quality gates"

    Write-Host "  Typecheck..."
    & pnpm typecheck
    if ($LASTEXITCODE -ne 0) { Write-Fail "Typecheck failed"; exit 1 }
    Write-Ok "Typecheck passed"

    Write-Host "  Frontend tests..."
    & pnpm --filter @memecho/desktop test
    if ($LASTEXITCODE -ne 0) { Write-Fail "Frontend tests failed"; exit 1 }
    Write-Ok "Frontend tests passed"
} else {
    Write-Warn "Tests skipped (-SkipTests)"
}

# ─── Build ───────────────────────────────────────────────────────────────────

Write-Step "Building frontend"
if (-not $SkipFrontend) {
    & pnpm --filter @memecho/desktop build
    if ($LASTEXITCODE -ne 0) { Write-Fail "Frontend build failed"; exit 1 }
    Write-Ok "Frontend built"
} else {
    Write-Warn "Frontend build skipped (-SkipFrontend)"
}

# Rust tests embed the frontend dist (generate_context), so they must run after
# the frontend build. Failures terminate the build.
if (-not $SkipTests) {
    Write-Step "Running Rust tests"
    Invoke-NativeCapture { & cargo test --manifest-path (Join-Path $RepoRoot "apps\desktop\src-tauri\Cargo.toml") --locked 2>&1 }
    if ($LASTEXITCODE -ne 0) { Write-Fail "Rust tests failed"; exit 1 }
    Write-Ok "Rust tests passed"
}

Write-Step "Building MSI and NSIS installers"
& pnpm --filter @memecho/desktop tauri build
if ($LASTEXITCODE -ne 0) { Write-Fail "Tauri build failed"; exit 1 }
Write-Ok "Tauri build completed"

# ─── Collect artifacts ───────────────────────────────────────────────────────

Write-Step "Collecting artifacts"

$bundleRoot = Join-Path $RepoRoot "apps\desktop\src-tauri\target\release\bundle"
$resolvedOutput = [IO.Path]::GetFullPath((Join-Path $RepoRoot $OutputDirectory))
New-Item -ItemType Directory -Force -Path $resolvedOutput | Out-Null

$artifacts = @(
    Get-ChildItem -Path (Join-Path $bundleRoot "msi") -Filter "*.msi" -File -ErrorAction SilentlyContinue
    Get-ChildItem -Path (Join-Path $bundleRoot "nsis") -Filter "*-setup.exe" -File -ErrorAction SilentlyContinue
)

if ($artifacts.Count -eq 0) {
    Write-Fail "No MSI or NSIS installer found in $bundleRoot"
    exit 1
}

$manifest = @()
foreach ($artifact in $artifacts) {
    $destination = Join-Path $resolvedOutput $artifact.Name
    Copy-Item -LiteralPath $artifact.FullName -Destination $destination -Force
    $hash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
    $size = (Get-Item -LiteralPath $destination).Length
    $signature = Get-AuthenticodeSignature -FilePath $destination
    $manifest += [pscustomobject]@{
        file = $artifact.Name
        sha256 = $hash
        size_bytes = $size
        size_mb = [math]::Round($size / 1MB, 2)
        signature_status = [string]$signature.Status
        version = $version
        commit = $commit
        gateway_url = $env:VITE_GATEWAY_URL
        built_at = (Get-Date -Format "o")
    }
    Write-Host ""
    Write-Host "  $($artifact.Name)" -ForegroundColor White
    Write-Host "    SHA-256:  $hash"
    Write-Host "    Size:     $([math]::Round($size / 1MB, 2)) MB"
    Write-Host "    Signed:   $($signature.Status)"
}

$manifestPath = Join-Path $resolvedOutput "build-manifest.json"
$manifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding utf8

Write-Host ""
Write-Host "=" * 60 -ForegroundColor DarkGray
Write-Host "  Build complete!" -ForegroundColor Green
Write-Host "=" * 60 -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Artifacts: $resolvedOutput"
Write-Host "  Manifest:  $manifestPath"
Write-Host "  Version:   $version"
Write-Host "  Commit:    $commit"
Write-Host ""
Write-Host "  NOTE: Installers are UNSIGNED. Mark clearly for roadshow use." -ForegroundColor Yellow
Write-Host ""
