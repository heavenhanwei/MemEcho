[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$GatewayUrl,

    [string]$OutputDirectory = "release-artifacts",

    [switch]$SkipTests,

    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-PublicGatewayUrl {
    param([string]$Value)

    $parsed = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$parsed)) {
        throw "GatewayUrl must be an absolute HTTPS URL."
    }
    if ($parsed.Scheme -ne "https") {
        throw "GatewayUrl must use HTTPS."
    }
    if (-not [string]::IsNullOrEmpty($parsed.UserInfo) -or
        -not [string]::IsNullOrEmpty($parsed.Query) -or
        -not [string]::IsNullOrEmpty($parsed.Fragment) -or
        ($parsed.AbsolutePath -ne "/" -and $parsed.AbsolutePath -ne "")) {
        throw "GatewayUrl must be a clean HTTPS origin without credentials, path, query, or fragment."
    }
    if ($parsed.IsLoopback) {
        throw "GatewayUrl must be a public production origin, not localhost."
    }
    return $parsed.GetLeftPart([UriPartial]::Authority)
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$gatewayOrigin = Assert-PublicGatewayUrl -Value $GatewayUrl
$corepack = (Get-Command corepack.cmd -ErrorAction Stop).Source

if (-not [string]::IsNullOrWhiteSpace($env:VITE_GATEWAY_TOKEN)) {
    throw "VITE_GATEWAY_TOKEN must be unset. Production access tokens must never be embedded in an installer."
}

Push-Location $repoRoot
try {
    $dirty = @(git status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect the Git worktree." }
    if ($dirty.Count -gt 0) {
        throw "Release builds require a clean tracked worktree. Commit or safely set aside tracked changes first."
    }

    $env:VITE_GATEWAY_URL = $gatewayOrigin
    Remove-Item Env:VITE_GATEWAY_TOKEN -ErrorAction SilentlyContinue

    Write-Host "Release gateway: $gatewayOrigin"
    Write-Host "No access token will be embedded. Provision it after installation in memEcho settings."

    if ($ValidateOnly) {
        Write-Host "Release inputs are valid. Packaging was not started."
        return
    }

    if (-not $SkipTests) {
        & $corepack pnpm typecheck
        if ($LASTEXITCODE -ne 0) { throw "Frontend typecheck failed." }
        & $corepack pnpm test
        if ($LASTEXITCODE -ne 0) { throw "Frontend tests failed." }
        cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --locked -- --test-threads=1
        if ($LASTEXITCODE -ne 0) { throw "Rust tests failed." }
    }

    & $corepack pnpm --filter '@memecho/desktop' tauri build
    if ($LASTEXITCODE -ne 0) { throw "Tauri installer build failed." }

    $resolvedOutput = [IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
    New-Item -ItemType Directory -Force -Path $resolvedOutput | Out-Null
    $bundleRoot = Join-Path $repoRoot "apps/desktop/src-tauri/target/release/bundle"
    $artifacts = @(
        Get-ChildItem -Path (Join-Path $bundleRoot "msi") -Filter "*.msi" -File -ErrorAction SilentlyContinue
        Get-ChildItem -Path (Join-Path $bundleRoot "nsis") -Filter "*-setup.exe" -File -ErrorAction SilentlyContinue
    )
    if ($artifacts.Count -eq 0) { throw "No MSI or NSIS installer was produced." }

    $manifest = foreach ($artifact in $artifacts) {
        $destination = Join-Path $resolvedOutput $artifact.Name
        Copy-Item -LiteralPath $artifact.FullName -Destination $destination -Force
        $signature = Get-AuthenticodeSignature -FilePath $destination
        [pscustomobject]@{
            file = $artifact.Name
            sha256 = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
            size_bytes = (Get-Item -LiteralPath $destination).Length
            signature_status = [string]$signature.Status
            gateway_origin = $gatewayOrigin
        }
    }
    $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $resolvedOutput "release-manifest.json") -Encoding utf8
    Write-Host "Installers and manifest are ready in $resolvedOutput"
}
finally {
    Pop-Location
}
