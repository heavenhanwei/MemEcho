# build-msi.ps1 — Build memEcho MSI/NSIS installers for roadshow or local testing.
#
# Simplified wrapper around `pnpm tauri build`. For production releases with
# full quality gates and clean-worktree enforcement, use build-windows-release.ps1.
#
# Usage:
#   .\scripts\build-msi.ps1                                  # local dev build
#   .\scripts\build-msi.ps1 -GatewayUrl https://gw.example.com  # baked-in URL
#   .\scripts\build-msi.ps1 -SkipFrontend                     # skip pnpm build
#   .\scripts\build-msi.ps1 -OutputDir C:\release             # custom output
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

# ─── Preflight ───────────────────────────────────────────────────────────────

Write-Step "Checking build prerequisites"

# Rust
$rustup = Get-Command rustup -ErrorAction SilentlyContinue
if ($rustup) {
    $rustVer = & rustc --version 2>&1
    Write-Ok "Rust: $rustVer"
} else {
    Write-Fail "Rust not found. Install from https://rustup.rs"
    exit 1
}

# pnpm
$pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
if ($pnpm) {
    $pnpmVer = & pnpm --version 2>&1
    Write-Ok "pnpm: v$pnpmVer"
} else {
    Write-Fail "pnpm not found."
    exit 1
}

# Tauri CLI
$tauriCli = Get-Command tauri -ErrorAction SilentlyContinue
if (-not $tauriCli) {
    # Try via pnpm
    $tauriCheck = & pnpm tauri --version 2>&1
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
$gitStatus = & git status --porcelain --untracked-files=no 2>&1
if ($gitStatus) {
    Write-Warn "Working tree has uncommitted changes (non-blocking for dev build)"
}

# ─── Gateway URL ─────────────────────────────────────────────────────────────

if ($GatewayUrl) {
    Write-Step "Baking gateway URL into build"
    $parsed = $null
    if (-not [Uri]::TryCreate($GatewayUrl, [UriKind]::Absolute, [ref]$parsed)) {
        Write-Fail "Invalid GatewayUrl: $GatewayUrl"
        exit 1
    }
    if ($parsed.Scheme -eq "http" -and -not ($parsed.Host -in @("localhost", "127.0.0.1", "[::1]"))) {
        Write-Fail "HTTP gateway URL only allowed for localhost. Use HTTPS for production."
        exit 1
    }
    $env:VITE_GATEWAY_URL = $parsed.GetLeftPart([UriPartial]::Authority)
    Write-Ok "VITE_GATEWAY_URL = $env:VITE_GATEWAY_URL"
} else {
    Write-Step "No gateway URL specified — will use runtime config"
    Remove-Item Env:VITE_GATEWAY_URL -ErrorAction SilentlyContinue
}

# Ensure no token is embedded
Remove-Item Env:VITE_GATEWAY_TOKEN -ErrorAction SilentlyContinue

# ─── Version info ────────────────────────────────────────────────────────────

Write-Step "Reading version info"

$tauriConf = Get-Content (Join-Path $RepoRoot "apps\desktop\src-tauri\tauri.conf.json") -Raw | ConvertFrom-Json
$version = $tauriConf.version
$commit = & git rev-parse --short HEAD 2>&1
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
    if ($LASTEXITCODE -ne 0) { Write-Warn "Some frontend tests failed (non-blocking for dev build)" }

    Write-Host "  Rust tests..."
    & cargo test --manifest-path (Join-Path $RepoRoot "apps\desktop\src-tauri\Cargo.toml") --locked 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warn "Some Rust tests failed (non-blocking for dev build)" }
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
        gateway_url = if ($env:VITE_GATEWAY_URL) { $env:VITE_GATEWAY_URL } else { "(runtime)" }
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
