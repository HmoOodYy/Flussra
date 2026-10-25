# Locate PostgreSQL test binaries for local and CI checks.
$ErrorActionPreference = "Stop"
$requiredBinaries = @("initdb.exe", "postgres.exe", "pg_ctl.exe")
$candidates = @(Get-Item -Path "C:\Program Files\PostgreSQL\*\bin" -ErrorAction SilentlyContinue |
    Where-Object { $_.PSIsContainer } | Sort-Object FullName -Descending)
$pgBin = $null
foreach ($candidate in $candidates) {
    $missing = @($requiredBinaries | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $candidate.FullName $_) -PathType Leaf)
    })
    if ($missing.Count -eq 0) {
        $pgBin = $candidate.FullName
        break
    }
}
if (-not $pgBin) { throw "PostgreSQL test binaries were not found under C:\Program Files\PostgreSQL." }
$env:PATH = $pgBin + [System.IO.Path]::PathSeparator + $env:PATH
if ($env:GITHUB_PATH) { Add-Content -LiteralPath $env:GITHUB_PATH -Value $pgBin -Encoding utf8 }
Write-Host "PostgreSQL test binaries: $pgBin"
