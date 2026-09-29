$ROOT = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$BACKEND = Join-Path $ROOT "backend"
$PYTHON = Join-Path $BACKEND ".venv\Scripts\python.exe"
$SETUP_SCRIPT = Join-Path $ROOT "scripts\setup_backend.ps1"

if (-not (Test-Path $PYTHON)) {
    Write-Host "ERROR: This worktree's backend virtual environment is missing. Run $SETUP_SCRIPT first." -ForegroundColor Red
    exit 1
}

$testDirectory = Join-Path $BACKEND "tests"
if (-not (Test-Path $testDirectory)) {
    Write-Host "ERROR: Backend test directory not found at $testDirectory." -ForegroundColor Red
    exit 1
}

$pythonVersion = (& $PYTHON -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Could not run this worktree's backend Python at $PYTHON." -ForegroundColor Red
    exit 1
}

$testFileCount = @(Get-ChildItem -Path $testDirectory -Filter "test_*.py" -Recurse -File).Count
Write-Host "Python executable: $PYTHON"
Write-Host "Python version: $pythonVersion"
Write-Host "Backend test files: $testFileCount"

$pytestArgs = @("-m", "pytest", "-ra", "--tb=short") + @($args)
Push-Location $BACKEND
try {
    & $PYTHON @pytestArgs
    $pytestExitCode = $LASTEXITCODE
} catch {
    Write-Host "ERROR: Failed to start pytest with $PYTHON. $($_.Exception.Message)" -ForegroundColor Red
    $pytestExitCode = 1
} finally {
    Pop-Location
}
exit $pytestExitCode
