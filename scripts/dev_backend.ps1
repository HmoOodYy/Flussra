# dev_backend.ps1
# ───────────────────────────────────────────────────────────────────────────
# One-shot dev startup: apply migrations THEN start uvicorn.
#
# Usage (from any directory):
#   .\scripts\dev_backend.ps1
#
# Why this script exists
# ─────────────────────
# Every time Claude (or any developer) adds an Alembic migration the dev DB
# needs `alembic upgrade head` before the backend will serve requests without
# 500 errors.  Forgetting that step is the #1 cause of recurring login 500s.
# Running this script instead of `uvicorn` directly eliminates that risk.
# ───────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

# Resolve project root relative to this script file
$ROOT = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PYTHON = Join-Path $ROOT "backend\.venv\Scripts\python.exe"

if (-not (Test-Path $PYTHON)) {
    Write-Error "Python venv not found at: $PYTHON`nRun: cd backend && python -m venv .venv && pip install -r requirements.txt"
    exit 1
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Payroll App v3 — Dev Backend Startup  " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Apply migrations ──────────────────────────────────────────────
Write-Host "[1/2] Applying Alembic migrations..." -ForegroundColor Yellow
Set-Location $ROOT
& $PYTHON -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    throw ("alembic upgrade head failed. Exit code: {0}. Fix migrations before starting the backend." -f $LASTEXITCODE)
}
}
Write-Host "      Migrations OK." -ForegroundColor Green
Write-Host ""

# ── Step 2: Start uvicorn ─────────────────────────────────────────────────
Write-Host "[2/2] Starting uvicorn on http://0.0.0.0:8000 ..." -ForegroundColor Yellow
Write-Host "      Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""
Set-Location (Join-Path $ROOT "backend")
& $PYTHON -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
