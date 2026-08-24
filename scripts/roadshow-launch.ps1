# roadshow-launch.ps1 — One-command launcher for memEcho roadshow demos.
#
# Starts the gateway in mock mode and the desktop dev server, copies demo
# samples into the gateway data directory, and prints a pre-flight checklist.
#
# Usage:
#   .\scripts\roadshow-launch.ps1                    # mock mode, default port
#   .\scripts\roadshow-launch.ps1 -SkipDesktop       # gateway only
#   .\scripts\roadshow-launch.ps1 -Provider bailian   # real Bailian backend
#
# Prerequisites:
#   - Python 3.12+ installed and on PATH
#   - Node.js 20+ and pnpm installed
#   - Gateway dependencies: pip install -e ".[dev]" (from services/gateway/)
#   - Frontend dependencies: pnpm install (from project root)

param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8787,
    [string]$Provider = "mock",
    [string]$Token = "roadshow-demo-token-2026",
    [switch]$SkipDesktop,
    [switch]$SkipPreflight
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$GatewayDir = Join-Path $ProjectDir "services\gateway"
$DemoDir = Join-Path $ProjectDir "packages\demo-samples\samples"

function Write-Step($msg) { Write-Host "`n>>> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  WARN: $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  FAIL: $msg" -ForegroundColor Red }

# ─── Preflight checks ───────────────────────────────────────────────────────

if (-not $SkipPreflight) {
    Write-Step "Running pre-flight checks"

    # Python
    $pythonPath = $null
    $projectPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $projectPython) { $pythonPath = $projectPython }
    if (-not $pythonPath) {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) { $pythonPath = $cmd.Source }
    }
    if (-not $pythonPath) {
        $localRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Programs\Python"
        foreach ($v in @("Python312", "Python313", "Python311")) {
            $c = Join-Path $localRoot "$v\python.exe"
            if (Test-Path -LiteralPath $c) { $pythonPath = $c; break }
        }
    }
    if ($pythonPath) {
        $pyVer = & $pythonPath --version 2>&1
        Write-Ok "Python: $pyVer ($pythonPath)"
    } else {
        Write-Fail "Python 3.12+ not found. Install or add to PATH."
        exit 1
    }

    # pnpm
    $pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
    if ($pnpm) {
        $pnpmVer = & pnpm --version 2>&1
        Write-Ok "pnpm: v$pnpmVer"
    } else {
        Write-Fail "pnpm not found. Install with: npm install -g pnpm"
        exit 1
    }

    # Gateway health (if already running)
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/health" -TimeoutSec 3 -ErrorAction Stop
        Write-Warn "Gateway already running on port $Port (provider=$($health.provider)). Will reuse."
        $gatewayAlreadyRunning = $true
    } catch {
        $gatewayAlreadyRunning = $false
        Write-Ok "Port $Port is free"
    }

    # Demo samples
    if (Test-Path $DemoDir) {
        $sampleCount = (Get-ChildItem -Directory $DemoDir).Count
        Write-Ok "Demo samples: $sampleCount available"
    } else {
        Write-Warn "Demo samples not found at $DemoDir"
    }

    # Disk space
    $drive = (Get-Item $ProjectDir).PSDrive
    if ($drive) {
        $freeGB = [math]::Round((Get-PSDrive $drive.Name).Free / 1GB, 1)
        if ($freeGB -lt 1) {
            Write-Warn "Low disk space: ${freeGB}GB free"
        } else {
            Write-Ok "Disk space: ${freeGB}GB free"
        }
    }
}

# ─── Environment setup ───────────────────────────────────────────────────────

Write-Step "Configuring environment"

$env:MEMECHO_PROVIDER = $Provider
$env:MEMECHO_DEMO_TOKEN = $Token
$env:MEMECHO_DATA_DIR = Join-Path $GatewayDir "tmp"
$env:MEMECHO_PUBLIC_BASE_URL = "http://127.0.0.1:$Port"
$gatewaySource = Join-Path $GatewayDir "src"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$gatewaySource;$env:PYTHONPATH" } else { $gatewaySource }

Write-Ok "Provider: $Provider"
Write-Ok "Port: $Port"
Write-Ok "Data dir: $($env:MEMECHO_DATA_DIR)"

# ─── Copy demo samples ──────────────────────────────────────────────────────

if (Test-Path $DemoDir) {
    Write-Step "Copying demo samples to gateway data directory"
    $targetDir = Join-Path $env:MEMECHO_DATA_DIR "demo"
    if (-not (Test-Path $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }
    Copy-Item -Path "$DemoDir\*" -Destination $targetDir -Recurse -Force
    Write-Ok "Demo samples copied to $targetDir"
}

# ─── Start gateway ───────────────────────────────────────────────────────────

if (-not $gatewayAlreadyRunning) {
    Write-Step "Starting gateway on http://127.0.0.1:$Port"
    $gatewayProcess = Start-Process -FilePath $pythonPath `
        -ArgumentList "-m", "uvicorn", "memecho_gateway.main:app", "--host", "127.0.0.1", "--port", "$Port" `
        -WorkingDirectory $GatewayDir `
        -PassThru `
        -NoNewWindow
    Write-Ok "Gateway PID: $($gatewayProcess.Id)"

    # Wait for health
    $ready = $false
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/health" -TimeoutSec 2 -ErrorAction Stop
            $ready = $true
            break
        } catch { }
    }
    if ($ready) {
        Write-Ok "Gateway is healthy (provider=$($health.provider), version=$($health.version))"
    } else {
        Write-Fail "Gateway did not become healthy within 15 seconds. Check logs."
        exit 1
    }
}

# ─── Pre-flight checklist ────────────────────────────────────────────────────

Write-Host ""
Write-Host "=" * 60 -ForegroundColor DarkGray
Write-Host "  memEcho Roadshow Pre-flight Checklist" -ForegroundColor White
Write-Host "=" * 60 -ForegroundColor DarkGray
Write-Host ""
Write-Host "  [1] Gateway:     http://127.0.0.1:$Port  (provider=$Provider)" -ForegroundColor White
Write-Host "  [2] Demo mode:   $($Provider -eq 'mock' ? 'MOCK (deterministic, no real ASR)' : 'REAL Bailian')" -ForegroundColor $(if ($Provider -eq 'mock') { 'Yellow' } else { 'Green' })
Write-Host "  [3] Token:       $(if ($Token.Length -gt 8) { $Token.Substring(0,4) + '****' + $Token.Substring($Token.Length-4) } else { '****' })" -ForegroundColor White
Write-Host "  [4] Samples:     $(if (Test-Path $DemoDir) { (Get-ChildItem -Directory $DemoDir).Count.ToString() + ' loaded' } else { 'none' })" -ForegroundColor White
Write-Host ""

if ($Provider -eq "mock") {
    Write-Host "  NOTE: Mock mode returns fixed demo subtitles." -ForegroundColor Yellow
    Write-Host "        Real subtitles require Bailian realtime ASR." -ForegroundColor Yellow
    Write-Host ""
}

# ─── Start desktop ───────────────────────────────────────────────────────────

if (-not $SkipDesktop) {
    Write-Step "Starting desktop dev server"
    Write-Host "  Press Ctrl+C to stop both gateway and desktop." -ForegroundColor DarkGray
    Write-Host ""
    Set-Location $ProjectDir
    & pnpm dev
} else {
    Write-Host ""
    Write-Host "Gateway is running. Desktop start skipped (-SkipDesktop)." -ForegroundColor Green
    Write-Host "To start manually: pnpm dev" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "Press Ctrl+C to stop the gateway." -ForegroundColor DarkGray
    try { $gatewayProcess | Wait-Process } finally { }
}
