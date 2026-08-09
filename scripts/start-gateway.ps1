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
    [int]$Port = 8787,
    [string]$Provider = "mock",
    [string]$Token = "change-me"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$GatewayDir = Join-Path (Split-Path -Parent $ScriptDir) "services" "gateway"

if (-not (Test-Path (Join-Path $GatewayDir "pyproject.toml"))) {
    Write-Error "Gateway not found at $GatewayDir — run this script from the project root."
    exit 1
}

# Check Python
try {
    $pyVersion = python --version 2>&1
    Write-Host "Using $pyVersion"
} catch {
    Write-Error "Python 3.12+ is required but not found on PATH."
    exit 1
}

# Set environment variables (does NOT modify services/gateway/.env)
$env:MEMECHO_PROVIDER = $Provider
$env:MEMECHO_DEMO_TOKEN = $Token
$env:MEMECHO_DATA_DIR = Join-Path $GatewayDir "tmp"
$env:MEMECHO_PUBLIC_BASE_URL = "http://127.0.0.1:$Port"

Write-Host ""
Write-Host "Starting memEcho gateway on http://127.0.0.1:$Port"
Write-Host "  Provider: $Provider"
Write-Host "  Token:    $(if ($Token.Length -gt 8) { $Token.Substring(0,4) + '****' + $Token.Substring($Token.Length-4) } else { '****' })"
Write-Host ""
Write-Host "Press Ctrl+C to stop."
Write-Host ""

Set-Location $GatewayDir
python -m uvicorn memecho_gateway.main:app --host 127.0.0.1 --port $Port
