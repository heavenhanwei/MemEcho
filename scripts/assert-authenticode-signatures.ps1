#Requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$Path
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$results = foreach ($candidate in $Path) {
    $resolved = Resolve-Path -LiteralPath $candidate -ErrorAction Stop
    $signature = Get-AuthenticodeSignature -LiteralPath $resolved.Path
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "Authenticode verification failed for '$($resolved.Path)': $($signature.Status) $($signature.StatusMessage)"
    }
    if ($null -eq $signature.SignerCertificate) {
        throw "No signer certificate was returned for '$($resolved.Path)'."
    }
    if ($null -eq $signature.TimeStamperCertificate) {
        throw "The signature on '$($resolved.Path)' has no trusted timestamp."
    }

    [pscustomobject]@{
        file = $resolved.Path
        status = [string]$signature.Status
        signer_subject = $signature.SignerCertificate.Subject
        signer_thumbprint = $signature.SignerCertificate.Thumbprint
        timestamp_subject = $signature.TimeStamperCertificate.Subject
    }
}

$results | Format-Table -AutoSize
