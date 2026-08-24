# start-gateway.ps1 — Start the memEcho analysis gateway for local development.
#
# Usage:
#   .\scripts\start-gateway.ps1                    # default port 8787, mock provider
#   .\scripts\start-gateway.ps1 -Port 9000         # custom port
#   .\scripts\start-gateway.ps1 -Provider bailian  # use real Bailian backend
#
# Prerequisites:
#   - Python 3.12+ installed and on PATH
#   - Gateway dependencies installed: pip install -e ".[dev]" (from services/gateway/)

param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8787,
    [string]$Provider = "mock",
    [string]$Token = "change-me"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$GatewayDir = Join-Path -Path $ProjectDir -ChildPath "services\gateway"

if (-not (Test-Path (Join-Path $GatewayDir "pyproject.toml"))) {
    Write-Error "Gateway not found at $GatewayDir — check that the memecho-desktop project is complete."
    exit 1
}

# Check Python. Prefer the project virtual environment so a stale Windows Store
# alias or broken PATH entry cannot prevent the local gateway from starting.
# Then try PATH and the standard per-user Python install locations.
$pythonPath = $null
$projectPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $projectPython) {
    $pythonPath = $projectPython
}
if (-not $pythonPath) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $pythonPath = $pythonCommand.Source
    }
}
if (-not $pythonPath) {
    $localPythonRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Programs\Python"
    foreach ($version in @("Python312", "Python313", "Python311")) {
        $candidate = Join-Path $localPythonRoot "$version\python.exe"
        if (Test-Path -LiteralPath $candidate) {
            $pythonPath = $candidate
            break
        }
    }
}
if (-not $pythonPath) {
    Write-Error "Python 3.12+ is required. Add Python to PATH or install it under %LocalAppData%\Programs\Python."
    exit 1
}
$pyVersion = & $pythonPath --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python was found at $pythonPath but could not be started."
    exit 1
}
Write-Host "Using $pyVersion ($pythonPath)"

# Set environment variables (does NOT modify services/gateway/.env)
$env:MEMECHO_PROVIDER = $Provider
$env:MEMECHO_DEMO_TOKEN = $Token
$env:MEMECHO_DATA_DIR = Join-Path $GatewayDir "tmp"
$env:MEMECHO_PUBLIC_BASE_URL = "http://127.0.0.1:$Port"
$gatewaySource = Join-Path $GatewayDir "src"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$gatewaySource;$env:PYTHONPATH" } else { $gatewaySource }

Write-Host ""
Write-Host "Starting memEcho gateway on http://127.0.0.1:$Port"
Write-Host "  Provider: $Provider"
Write-Host "  Token:    $(if ($Token.Length -gt 8) { $Token.Substring(0,4) + '****' + $Token.Substring($Token.Length-4) } else { '****' })"
Write-Host ""
Write-Host "Press Ctrl+C to stop."
Write-Host ""

Set-Location $GatewayDir
$gatewayJob = Start-Job -ScriptBlock {
    param($py, $dir, $p)
    Set-Location $dir
    & $py -m uvicorn memecho_gateway.main:app --host 127.0.0.1 --port $p
} -ArgumentList $pythonPath, $GatewayDir, $Port

# Health check: wait for gateway to become ready
$healthUrl = "http://127.0.0.1:$Port/v1/health"
$ready = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2 -ErrorAction Stop
        $ready = $true
        Write-Host ""
        Write-Host "Gateway is healthy." -ForegroundColor Green
        Write-Host "  Provider: $($health.provider)"
        if ($health.version) { Write-Host "  Version:  $($health.version)" }
        Write-Host ""
        break
    } catch { }
}
if (-not $ready) {
    Write-Host ""
    Write-Host "Gateway did not become healthy within 15 seconds. It may still be starting." -ForegroundColor Yellow
    Write-Host "Check the gateway window for errors." -ForegroundColor Yellow
    Write-Host ""
}

# Wait for the gateway process; print exit message when it stops
try {
    Receive-Job $gatewayJob -Wait
} finally {
    Remove-Job $gatewayJob -Force -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host "Gateway has stopped." -ForegroundColor Cyan
}
