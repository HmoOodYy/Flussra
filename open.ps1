# open.ps1
# One command to start the full dev + Cloudflare tunnel stack.
#
# Usage (from project root):
#   .\open.ps1
#
# What it does:
#   1. Starts the backend in a new window (applies migrations, then uvicorn).
#   2. Waits until http://localhost:8000 is reachable.
#   3. Starts a Cloudflare Quick Tunnel for the backend (silent, log file).
#   4. Captures the backend tunnel URL.
#   5. Starts the Vite frontend in a new window with VITE_API_BASE_URL set.
#   6. Waits until http://localhost:5173 is reachable.
#   7. Starts a Cloudflare Quick Tunnel for the frontend (silent, log file).
#   8. Captures the frontend tunnel URL.
#   9. Prints the public URL and copies it to the clipboard.
#
# Logs: .dev-tunnel\
# Close backend / frontend windows manually when done.

$ErrorActionPreference = "Stop"

$ROOT        = Split-Path -Parent $MyInvocation.MyCommand.Path
$SCRIPTS     = Join-Path $ROOT "scripts"
$FRONTEND    = Join-Path $ROOT "frontend"
$LOG_DIR     = Join-Path $ROOT ".dev-tunnel"

$BACKEND_URL  = "http://localhost:8000"
$FRONTEND_URL = "http://localhost:5173"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Find-Cloudflared {
    if ($env:CLOUDFLARED_PATH -and (Test-Path $env:CLOUDFLARED_PATH)) {
        return $env:CLOUDFLARED_PATH
    }
    $known = "G:\Tools\cloudflared\cloudflared.exe"
    if (Test-Path $known) { return $known }
    $onPath = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return $null
}

function Wait-ForHttp {
    param([string]$Url, [string]$Label, [int]$Timeout = 90)
    $deadline = (Get-Date).AddSeconds($Timeout)
    Write-Host "  Waiting for $Label ..." -ForegroundColor Yellow
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
            if ($r.StatusCode -lt 500) {
                Write-Host "  $Label ready." -ForegroundColor Green
                return
            }
        } catch { }
        Start-Sleep -Seconds 2
    }
    Write-Error "$Label did not become reachable at $Url within $Timeout s."
    exit 1
}

function Wait-ForTunnelUrl {
    param(
        [string]$OutLog,
        [string]$ErrLog,
        [string]$Label,
        [int]$Timeout = 90
    )
    $deadline = (Get-Date).AddSeconds($Timeout)
    Write-Host "  Waiting for $Label tunnel URL ..." -ForegroundColor Yellow
    $pattern = "https://[a-z0-9-]+\.trycloudflare\.com"
    while ((Get-Date) -lt $deadline) {
        foreach ($f in @($OutLog, $ErrLog)) {
            if (Test-Path $f) {
                $txt = Get-Content $f -Raw -ErrorAction SilentlyContinue
                if ($txt -and ($txt -match $pattern)) {
                    Write-Host "  $Label tunnel URL found." -ForegroundColor Green
                    return $Matches[0]
                }
            }
        }
        Start-Sleep -Seconds 2
    }
    Write-Host "Timeout. Check logs:" -ForegroundColor Red
    Write-Host "  $ErrLog" -ForegroundColor Red
    Write-Error "$Label tunnel URL not found within $Timeout s."
    exit 1
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

$cloudflared = Find-Cloudflared
if (-not $cloudflared) {
    Write-Host ""
    Write-Host "ERROR: cloudflared not found." -ForegroundColor Red
    Write-Host "Install from: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    Write-Host "Or set: " -NoNewline
    Write-Host '$env:CLOUDFLARED_PATH = "path\to\cloudflared.exe"' -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path $LOG_DIR)) {
    New-Item -ItemType Directory -Path $LOG_DIR | Out-Null
}

Write-Host ""
Write-Host "========================================"
Write-Host "  Payroll App v3 - Dev Tunnel Launcher"
Write-Host "========================================"
Write-Host ""
Write-Host "  cloudflared : $cloudflared"
Write-Host "  logs        : $LOG_DIR"
Write-Host ""

# ---------------------------------------------------------------------------
# Step 1 - Backend
# ---------------------------------------------------------------------------

Write-Host "[1/5] Starting backend ..." -ForegroundColor Cyan
$backendPs1 = Join-Path $SCRIPTS "dev_backend.ps1"
Start-Process powershell -ArgumentList @("-NoExit", "-File", $backendPs1)

Wait-ForHttp -Url "$BACKEND_URL/openapi.json" -Label "Backend" -Timeout 90

# ---------------------------------------------------------------------------
# Step 2 - Backend tunnel
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "[2/5] Starting backend tunnel ..." -ForegroundColor Cyan

$beOutLog = Join-Path $LOG_DIR "backend-out.log"
$beErrLog = Join-Path $LOG_DIR "backend-err.log"
"" | Out-File $beOutLog -Encoding utf8
"" | Out-File $beErrLog -Encoding utf8

$null = Start-Process -FilePath $cloudflared `
    -ArgumentList @("tunnel", "--url", $BACKEND_URL) `
    -NoNewWindow -PassThru `
    -RedirectStandardOutput $beOutLog `
    -RedirectStandardError  $beErrLog

$BackendTunnelUrl = Wait-ForTunnelUrl -OutLog $beOutLog -ErrLog $beErrLog -Label "Backend" -Timeout 90

Write-Host ""
Write-Host "  Backend tunnel: $BackendTunnelUrl" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# Step 3 - Frontend
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "[3/5] Starting frontend ..." -ForegroundColor Cyan

# Write a small temp script so we avoid inline quoting issues.
$feTmpPs1 = Join-Path $LOG_DIR "start-frontend.ps1"
$feTmpContent = @(
    "`$env:VITE_API_BASE_URL = '$BackendTunnelUrl'"
    "Set-Location '$FRONTEND'"
    "npm run dev -- --host 0.0.0.0"
)
$feTmpContent -join "`n" | Out-File -FilePath $feTmpPs1 -Encoding utf8

Start-Process powershell -ArgumentList @("-NoExit", "-File", $feTmpPs1)

Wait-ForHttp -Url $FRONTEND_URL -Label "Frontend" -Timeout 120

# ---------------------------------------------------------------------------
# Step 4 - Frontend tunnel
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "[4/5] Starting frontend tunnel ..." -ForegroundColor Cyan

$feOutLog = Join-Path $LOG_DIR "frontend-out.log"
$feErrLog = Join-Path $LOG_DIR "frontend-err.log"
"" | Out-File $feOutLog -Encoding utf8
"" | Out-File $feErrLog -Encoding utf8

$null = Start-Process -FilePath $cloudflared `
    -ArgumentList @("tunnel", "--url", $FRONTEND_URL) `
    -NoNewWindow -PassThru `
    -RedirectStandardOutput $feOutLog `
    -RedirectStandardError  $feErrLog

$FrontendTunnelUrl = Wait-ForTunnelUrl -OutLog $feOutLog -ErrLog $feErrLog -Label "Frontend" -Timeout 90

# ---------------------------------------------------------------------------
# Step 5 - Done
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "[5/5] All services running." -ForegroundColor Green
Write-Host ""
Write-Host "========================================"
Write-Host "PUBLIC TEST URL"
Write-Host $FrontendTunnelUrl -ForegroundColor Green
Write-Host "========================================"
Write-Host ""
Write-Host "Backend tunnel : $BackendTunnelUrl"
Write-Host "Logs           : $LOG_DIR"
Write-Host ""

try {
    Set-Clipboard $FrontendTunnelUrl
    Write-Host "Frontend URL copied to clipboard." -ForegroundColor Green
} catch {
    Write-Host "Could not copy to clipboard. URL above." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Close the backend and frontend windows when you are done testing."
Write-Host "Tunnels stop automatically when those windows close."
Write-Host ""
