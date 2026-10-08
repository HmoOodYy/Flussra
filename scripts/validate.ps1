$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$BACKEND = Join-Path $ROOT "backend"
$FRONTEND = Join-Path $ROOT "frontend"
if ($IsWindows -or $env:OS -eq "Windows_NT") {
    $PYTHON = Join-Path $BACKEND ".venv\Scripts\python.exe"
} else {
    $PYTHON = Join-Path $BACKEND ".venv/bin/python"
}

function Stop-Validation([string]$Gate, [string]$Message, [int]$ExitCode = 1) {
    Write-Host "FAILED: $Gate — $Message" -ForegroundColor Red
    exit $ExitCode
}

function Invoke-ValidationGate(
    [string]$Name,
    [string]$WorkingDirectory,
    [string]$Executable,
    [string[]]$Arguments
) {
    Write-Host "`n=== GATE: $Name ===" -ForegroundColor Cyan
    Write-Host "Command: $Executable $($Arguments -join ' ')"
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    Push-Location $WorkingDirectory
    try {
        & $Executable @Arguments
        $gateExitCode = $LASTEXITCODE
    } catch {
        Write-Host "ERROR: Could not start required command: $($_.Exception.Message)" -ForegroundColor Red
        $gateExitCode = 1
    } finally {
        Pop-Location
        $timer.Stop()
    }

    if ($gateExitCode -ne 0) {
        Stop-Validation $Name "child command exited $gateExitCode" $gateExitCode
    }
    Write-Host "PASSED: $Name ($([math]::Round($timer.Elapsed.TotalSeconds, 2))s)" -ForegroundColor Green
}

$script:validationTempState = $null
$script:validationPycachePrefix = $null
$script:validationPycacheCreated = $false
$script:validationCleanupFailed = $false

try {
    Write-Host "Flussra canonical local validation" -ForegroundColor Cyan

    . (Join-Path $ROOT "scripts\validation_temp.ps1")
    try {
        $script:validationTempState = Enter-FlussraValidationTemp
    } catch {
        Stop-Validation "Temporary directory" "validation temp root is not usable: $($_.Exception.Message)"
    }
    $validationTemp = $script:validationTempState.Root
    Write-Host "Temporary directory: $validationTemp (process-scoped TEMP/TMP; write probe passed)" -ForegroundColor Green

if (-not (Test-Path -LiteralPath $PYTHON -PathType Leaf)) {
    Stop-Validation "Backend tooling" "worktree backend Python is missing at $PYTHON. Run scripts/setup_backend.ps1 first."
}

$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodeCommand) {
    Stop-Validation "Frontend tooling" "Node.js was not found on PATH. Install the supported Node.js runtime and retry."
}
$npmCommand = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npmCommand) {
    Stop-Validation "Frontend tooling" "npm was not found on PATH. Install npm with Node.js and retry."
}

foreach ($package in @("eslint", "typescript", "vite")) {
    $packageJson = Join-Path $FRONTEND "node_modules\$package\package.json"
    if (-not (Test-Path -LiteralPath $packageJson -PathType Leaf)) {
        Stop-Validation "Frontend dependencies" "required package '$package' is missing. Run 'npm ci' in frontend and retry."
    }
}

Invoke-ValidationGate "Backend tooling availability" $BACKEND $PYTHON @(
    "-c", "import pytest, ruff, alembic"
)

Invoke-ValidationGate "Backend full test suite" $BACKEND $PYTHON @(
    "-m", "pytest", "-ra", "--tb=short"
)

Invoke-ValidationGate "Backend Ruff" $BACKEND $PYTHON @(
    "-m", "ruff", "check", "--no-cache", "tests"
)

$script:validationPycachePrefix = Join-Path ([System.IO.Path]::GetTempPath()) ("flussra-validation-pycache-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $script:validationPycachePrefix | Out-Null
$script:validationPycacheCreated = $true
Invoke-ValidationGate "Backend compileall" $BACKEND $PYTHON @(
    "-X", "pycache_prefix=$script:validationPycachePrefix", "-m", "compileall", "-q", "app", "tests"
)

Write-Host "`n=== GATE: Alembic single head (expected 0085) ===" -ForegroundColor Cyan
Write-Host "Command: $PYTHON -m alembic heads"
$alembicTimer = [System.Diagnostics.Stopwatch]::StartNew()
Push-Location $ROOT
try {
    $alembicOutput = @(& $PYTHON -m alembic heads 2>&1)
    $alembicExitCode = $LASTEXITCODE
} catch {
    Write-Host "ERROR: Could not start Alembic: $($_.Exception.Message)" -ForegroundColor Red
    $alembicExitCode = 1
    $alembicOutput = @()
} finally {
    Pop-Location
    $alembicTimer.Stop()
}
$alembicOutput | ForEach-Object { Write-Output $_ }
if ($alembicExitCode -ne 0) {
    Stop-Validation "Alembic heads" "child command exited $alembicExitCode" $alembicExitCode
}
$heads = @(
    foreach ($line in $alembicOutput) {
        if ([string]$line -match '^\s*(?<revision>[A-Za-z0-9_-]+)\s+\(head\)\s*$') {
            $Matches.revision
        }
    }
)
if ($heads.Count -ne 1 -or $heads[0] -ne "0085") {
    Stop-Validation "Alembic heads" "expected exactly one head 0085; found: $($heads -join ', ')"
}
Write-Host "PASSED: Alembic single head 0085 ($([math]::Round($alembicTimer.Elapsed.TotalSeconds, 2))s)" -ForegroundColor Green

$nodeExecutable = if ($nodeCommand.Source) { $nodeCommand.Source } else { $nodeCommand.Path }
$npmExecutable = if ($npmCommand.Source) { $npmCommand.Source } else { $npmCommand.Path }
$frontendTests = @(Get-ChildItem -LiteralPath (Join-Path $FRONTEND "tests") -Filter "*.test.ts" -File | Sort-Object Name)
if ($frontendTests.Count -eq 0) {
    Stop-Validation "Frontend full test suite" "no tests/*.test.ts files were found"
}
$frontendTestArguments = @("--test") + @($frontendTests | ForEach-Object { "tests/$($_.Name)" })
Invoke-ValidationGate "Frontend full test suite ($($frontendTests.Count) files)" $FRONTEND $nodeExecutable $frontendTestArguments
Invoke-ValidationGate "Frontend lint" $FRONTEND $npmExecutable @("run", "lint")
Invoke-ValidationGate "Frontend production build" $FRONTEND $npmExecutable @("run", "build")

Write-Host "`nALL REQUIRED LOCAL VALIDATION GATES PASSED." -ForegroundColor Green
} finally {
    try {
        if ($script:validationPycacheCreated -and (Test-Path -LiteralPath $script:validationPycachePrefix -PathType Container)) {
            Remove-Item -LiteralPath $script:validationPycachePrefix -Recurse -Force
        }
    } catch {
        $script:validationCleanupFailed = $true
        Write-Host "FAILED: Temporary artifact cleanup — could not remove invocation-owned cache '$script:validationPycachePrefix': $($_.Exception.Message)" -ForegroundColor Red
    } finally {
        if ($null -ne $script:validationTempState) {
            try {
                Exit-FlussraValidationTemp $script:validationTempState
                Write-Host "Restored caller TEMP/TMP and cleaned validation-owned cache." -ForegroundColor Green
            } catch {
                $script:validationCleanupFailed = $true
                Write-Host "FAILED: Environment restoration — $($_.Exception.Message)" -ForegroundColor Red
            }
        }
    }
}

if ($script:validationCleanupFailed) {
    Stop-Validation "Validation cleanup" "temporary artifact cleanup or caller environment restoration failed"
}
