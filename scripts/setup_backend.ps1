$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$BACKEND = Join-Path $ROOT "backend"
$VENV = Join-Path $BACKEND ".venv"
$VENV_PYTHON = Join-Path $VENV "Scripts\python.exe"
$SETUP_SCRIPT = Join-Path $ROOT "scripts\setup_backend.ps1"

if (-not (Test-Path (Join-Path $BACKEND "pyproject.toml"))) {
    Write-Host "ERROR: backend/pyproject.toml was not found under $ROOT" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $VENV_PYTHON)) {
    $candidates = @()
    $seenCandidatePaths = @{}
    foreach ($command in @(Get-Command py -All -ErrorAction SilentlyContinue)) {
        if ($command.CommandType -ne "Application") { continue }
        $candidatePath = $command.Source
        if (-not $candidatePath) { $candidatePath = $command.Path }
        if (-not $candidatePath -or $seenCandidatePaths.ContainsKey($candidatePath)) { continue }
        $seenCandidatePaths[$candidatePath] = $true
        $candidates += [pscustomobject]@{ Path = $candidatePath; Prefix = @("-3") }
    }
    foreach ($command in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
        if ($command.CommandType -ne "Application") { continue }
        $candidatePath = $command.Source
        if (-not $candidatePath) { $candidatePath = $command.Path }
        if (-not $candidatePath -or $seenCandidatePaths.ContainsKey($candidatePath)) { continue }
        $seenCandidatePaths[$candidatePath] = $true
        $candidates += [pscustomobject]@{ Path = $candidatePath; Prefix = @() }
    }

    $selected = $null
    $versionCode = "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    foreach ($candidate in $candidates) {
        try {
            $probeArgs = @($candidate.Prefix) + @("-c", $versionCode)
            $candidateVersion = (& $candidate.Path @probeArgs 2>$null | Out-String).Trim()
            if ($LASTEXITCODE -ne 0) { continue }
            $parsedVersion = [version]::Parse($candidateVersion)
            if ($parsedVersion -ge [version]"3.12") {
                $selected = $candidate
                $selectedVersion = $candidateVersion
                break
            }
        } catch {
            continue
        }
    }

    if (-not $selected) {
        Write-Host "ERROR: Python 3.12 or newer was not found. Install Python 3.12+ and make it available as 'py -3' or 'python', then rerun $SETUP_SCRIPT." -ForegroundColor Red
        exit 1
    }

    Write-Host "Selected base Python: $($selected.Path)"
    Write-Host "Base Python version: $selectedVersion"
    Write-Host "Creating worktree-local backend environment..." -ForegroundColor Cyan
    $createArgs = @($selected.Prefix) + @("-m", "venv", $VENV)
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $selected.Path @createArgs
        $createExitCode = $LASTEXITCODE
    } catch {
        Write-Host "ERROR: Failed to start Python while creating $VENV. $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($createExitCode -ne 0) {
        Write-Host "ERROR: Failed to create the backend virtual environment at $VENV." -ForegroundColor Red
        exit $createExitCode
    }
}

if (-not (Test-Path $VENV_PYTHON)) {
    Write-Host "ERROR: The local virtual environment does not contain $VENV_PYTHON." -ForegroundColor Red
    exit 1
}

try {
    $venvVersion = (& $VENV_PYTHON -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Could not run the local virtual environment's Python." }
    $parsedVenvVersion = [version]::Parse($venvVersion)
} catch {
    Write-Host "ERROR: Could not verify Python in $VENV_PYTHON. Recreate this worktree's backend/.venv with $SETUP_SCRIPT." -ForegroundColor Red
    exit 1
}

if ($parsedVenvVersion -lt [version]"3.12") {
    Write-Host "ERROR: $VENV_PYTHON is Python $venvVersion; backend requires Python 3.12 or newer. Remove this worktree's backend/.venv and rerun $SETUP_SCRIPT." -ForegroundColor Red
    exit 1
}

Write-Host "Backend Python: $VENV_PYTHON"
Write-Host "Python version: $venvVersion"
Write-Host 'Installing backend editable with the [dev] dependencies...'
$previousErrorActionPreference = $ErrorActionPreference
$installExitCode = 1
Push-Location $BACKEND
try {
    $ErrorActionPreference = "Continue"
    & $VENV_PYTHON -m pip install -e ".[dev]"
    $installExitCode = $LASTEXITCODE
} catch {
    Write-Host "ERROR: Failed to start pip in $VENV_PYTHON. $($_.Exception.Message)" -ForegroundColor Red
} finally {
    $ErrorActionPreference = $previousErrorActionPreference
    Pop-Location
}
if ($installExitCode -ne 0) {
    Write-Host "ERROR: Backend editable installation failed with exit code $installExitCode." -ForegroundColor Red
    exit $installExitCode
}

Write-Host "Backend environment is ready for this worktree." -ForegroundColor Green
