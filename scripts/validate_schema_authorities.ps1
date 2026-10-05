$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PYTHON = Join-Path $ROOT "backend\.venv\Scripts\python.exe"
$COMPARATOR = Join-Path $ROOT "scripts\compare_schema_authorities.py"
. (Join-Path $ROOT "scripts\validation_temp.ps1")

$tempState = $null
$locationPushed = $false
$comparisonExitCode = 1
$cleanupFailed = $false
try {
    $tempState = Enter-FlussraValidationTemp
    Write-Host "Temporary directory: $($tempState.Root) (process-scoped TEMP/TMP; write probe passed)" -ForegroundColor Green
    if (-not (Test-Path -LiteralPath $PYTHON -PathType Leaf)) {
        throw "Backend Python is missing at $PYTHON. Run scripts/setup_backend.ps1 first."
    }
    Push-Location $ROOT
    $locationPushed = $true
    & $PYTHON $COMPARATOR
    $comparisonExitCode = $LASTEXITCODE
} catch {
    Write-Host "FAILED: Schema Authority Equivalence — $($_.Exception.Message)" -ForegroundColor Red
    $comparisonExitCode = 1
} finally {
    if ($locationPushed) {
        try {
            Pop-Location
        } catch {
            $cleanupFailed = $true
            Write-Host "FAILED: Location restoration — $($_.Exception.Message)" -ForegroundColor Red
        }
    }
    if ($null -ne $tempState) {
        try {
            Exit-FlussraValidationTemp $tempState
            Write-Host "Restored caller TEMP/TMP." -ForegroundColor Green
        } catch {
            $cleanupFailed = $true
            Write-Host "FAILED: Temporary environment restoration — $($_.Exception.Message)" -ForegroundColor Red
        }
    }
}

if ($cleanupFailed) {
    exit 1
}
exit $comparisonExitCode
