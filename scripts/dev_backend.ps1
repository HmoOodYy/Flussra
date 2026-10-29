# dev_backend.ps1
# ───────────────────────────────────────────────────────────────────────────
# One-shot dev startup: apply migrations THEN start uvicorn.
#
# Usage (from any directory):
#   .\scripts\dev_backend.ps1
#
# Why this script exists
# ─────────────────────
# Every time a developer adds an Alembic migration the dev DB
# needs `alembic upgrade head` before the backend will serve requests without
# 500 errors.  Forgetting that step is the #1 cause of recurring login 500s.
# Running this script instead of `uvicorn` directly eliminates that risk.
# ───────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

# Resolve project root relative to this script file
$ROOT = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PYTHON = if ($env:FLUSSRA_PYTHON) {
    $env:FLUSSRA_PYTHON
} else {
    Join-Path $ROOT "backend\.venv\Scripts\python.exe"
}
$SETUP_SCRIPT = Join-Path $ROOT "scripts\setup_backend.ps1"
$ENV_FILE = $null
if ($env:FLUSSRA_ENV_FILE) {
    if (-not (Test-Path -LiteralPath $env:FLUSSRA_ENV_FILE -PathType Leaf)) {
        Write-Host "Backend environment file not found at: $env:FLUSSRA_ENV_FILE" -ForegroundColor Red
        exit 1
    }
    $ENV_FILE = (Resolve-Path -LiteralPath $env:FLUSSRA_ENV_FILE).Path
}

if (-not (Test-Path -LiteralPath $PYTHON -PathType Leaf)) {
    Write-Host "Python runtime not found at: $PYTHON`nRun $SETUP_SCRIPT or set FLUSSRA_PYTHON to an existing Python 3.12+ environment." -ForegroundColor Red
    exit 1
}

function Invoke-BackendPython {
    param([string[]]$PythonArgs)

    if ($ENV_FILE) {
        & $PYTHON -c "import runpy, sys; from dotenv import load_dotenv; load_dotenv(sys.argv[1], override=False); module_args = sys.argv[2:]; sys.argv = module_args[1:]; runpy.run_module(module_args[1], run_name='__main__')" $ENV_FILE @PythonArgs
    } else {
        & $PYTHON @PythonArgs
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Payroll App v3 — Dev Backend Startup  " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Apply migrations ──────────────────────────────────────────────
Write-Host "[1/2] Applying Alembic migrations..." -ForegroundColor Yellow
Set-Location $ROOT
Invoke-BackendPython -PythonArgs @("-m", "alembic", "upgrade", "head")
if ($LASTEXITCODE -ne 0) {
    throw ("alembic upgrade head failed. Exit code: {0}. Fix migrations before starting the backend." -f $LASTEXITCODE)
}
Write-Host "      Migrations OK." -ForegroundColor Green
Write-Host ""

# ── Step 2: Start uvicorn ─────────────────────────────────────────────────
Write-Host "[2/2] Starting uvicorn on http://0.0.0.0:8000 ..." -ForegroundColor Yellow
Write-Host "      Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""
Set-Location (Join-Path $ROOT "backend")
Invoke-BackendPython -PythonArgs @("-m", "uvicorn", "app.main:create_app", "--factory", "--reload", "--host", "0.0.0.0", "--port", "8000")
