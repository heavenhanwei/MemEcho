# contract-check.ps1 — Verify that frontend and gateway contracts are in sync.
#
# Checks:
#   1. Generated TypeScript types are up-to-date (generate_types.py --check)
#   2. Key enum values in generated.ts match the gateway Pydantic models
#   3. Required fields in AnalysisResult are present
#
# Usage:
#   .\scripts\contract-check.ps1              # full check
#   .\scripts\contract-check.ps1 -Fix         # regenerate types and verify

param(
    [switch]$Fix
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir

function Write-Step($msg) { Write-Host "`n>>> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  OK: $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "  FAIL: $msg" -ForegroundColor Red }

# ─── Find Python ─────────────────────────────────────────────────────────────

$pythonPath = $null
$projectPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $projectPython) { $pythonPath = $projectPython }
if (-not $pythonPath) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $pythonPath = $cmd.Source }
}
if (-not $pythonPath) {
    Write-Fail "Python not found."
    exit 1
}

# ─── Step 1: Generate and check types ────────────────────────────────────────

Write-Step "Checking generated TypeScript types"

$genScript = Join-Path $ProjectDir "services\gateway\scripts\generate_types.py"
$generatedFile = Join-Path $ProjectDir "packages\contracts\src\generated.ts"

if ($Fix) {
    Write-Host "  Regenerating types..."
    & $pythonPath $genScript
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Type generation failed."
        exit 1
    }
    Write-Ok "Types regenerated."
}

& $pythonPath $genScript --check
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Generated types are out of date. Run with -Fix to regenerate."
    exit 1
}
Write-Ok "Generated types are up-to-date."

# ─── Step 2: Check required AnalysisResult fields ────────────────────────────

Write-Step "Checking AnalysisResult schema completeness"

$content = Get-Content $generatedFile -Raw

$requiredFields = @(
    "schema_version",
    "request_id",
    "analysis_mode",
    "scope",
    "minutes",
    "content_analysis",
    "participants",
    "vad_series",
    "interaction_events",
    "self_echo",
    "coaching",
    "insights",
    "evidence",
    "uncertainties",
    "provenance",
    "memory"
)

$missingFields = @()
foreach ($field in $requiredFields) {
    if ($content -notmatch $field) {
        $missingFields += $field
    }
}

if ($missingFields.Count -gt 0) {
    Write-Fail "Missing fields in AnalysisResult: $($missingFields -join ', ')"
    exit 1
}
Write-Ok "All $($requiredFields.Count) required fields present in AnalysisResult."

# ─── Step 3: Check key enum values ───────────────────────────────────────────

Write-Step "Checking enum consistency"

$enumChecks = @{
    'ProcessingStage' = @('"queued"', '"running"', '"succeeded"', '"failed"', '"skipped"')
    'AnalysisMode' = @('"connected_full"', '"local_enhanced"', '"text_only"', '"insufficient"')
}

foreach ($enum in $enumChecks.GetEnumerator()) {
    $name = $enum.Key
    $values = $enum.Value
    if ($content -match "export type $name") {
        $allFound = $true
        foreach ($val in $values) {
            if ($content -notmatch [regex]::Escape($val)) {
                Write-Fail "${name} missing value: $val"
                $allFound = $false
            }
        }
        if ($allFound) {
            Write-Ok "${name} - all $($values.Count) values present"
        }
    } else {
        Write-Fail "Enum ${name} not found in generated types."
        exit 1
    }
}

# ─── Done ────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "All contract checks passed." -ForegroundColor Green
