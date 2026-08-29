#Requires -Version 5.1
<#
.SYNOPSIS
    Skeleton: package the Python gateway as a standalone sidecar executable.

.DESCRIPTION
    The desktop Gateway Supervisor expects a self-contained `memecho-gateway`
    executable that honors the startup handshake contract (see
    docs/open-source-edition/gateway-sidecar.md):

      - reads MEMECHO_GATEWAY_HOST / MEMECHO_GATEWAY_PORT / MEMECHO_GATEWAY_TOKEN
      - binds 127.0.0.1:$MEMECHO_GATEWAY_PORT
      - answers GET /v1/health with {"status":"ok","version":"...","protocol_version":1}

    This script is intentionally a skeleton: the Python gateway does not yet
    have a frozen-executable build. When that lands, wire it up here and copy
    the result to apps/desktop/src-tauri/binaries/memecho-gateway-<target-triple>.exe.

    SECURITY: never embed .env files, API keys, or tokens in the sidecar.
    The one-time access token is injected by the desktop at spawn time.
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$GatewayDir = Join-Path $RepoRoot "services/gateway"
$OutDir = Join-Path $RepoRoot "apps/desktop/src-tauri/binaries"

Write-Host "Gateway source: $GatewayDir"

$packager = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $packager) {
    Write-Error @"
PACKAGING BLOCKER: no standalone gateway build is available yet.

  - Install a Python freezer (e.g. 'pip install pyinstaller') and implement
    the freeze spec for services/gateway, or
  - Provide a prebuilt memecho-gateway executable via MEMECHO_GATEWAY_SIDECAR.

The desktop app keeps working in dev/external gateway mode until then.
"@
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# TODO(packaging): freeze the gateway entry point, e.g.
#   pyinstaller --onefile --name memecho-gateway ... memecho_gateway/main.py
# then:
#   Copy-Item <frozen exe> (Join-Path $OutDir "memecho-gateway-$TargetTriple.exe")
Write-Error "PACKAGING BLOCKER: freeze step not implemented yet (see docs/open-source-edition/gateway-sidecar.md)."
