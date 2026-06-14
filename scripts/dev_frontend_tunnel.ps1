# dev_frontend_tunnel.ps1
# Start the Vite dev server pointed at a tunnelled backend.
#
# Usage:
#   .\scripts\dev_frontend_tunnel.ps1 -BackendUrl "https://BACKEND.trycloudflare.com"
#
# Sets VITE_API_BASE_URL so apiClient calls the backend tunnel.
# Vite starts on 0.0.0.0 so a frontend tunnel can reach it.

param(
    [Parameter(Mandatory = $true)]
    [string]$BackendUrl
)

$ErrorActionPreference = "Stop"

$BackendUrl = $BackendUrl.TrimEnd('/')

if ($BackendUrl -notmatch '^https?://') {
    Write-Host "ERROR: BackendUrl must start with http:// or https://" -ForegroundColor Red
    exit 1
}

$ROOT = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$FRONTEND = Join-Path $ROOT "frontend"

if (-not (Test-Path (Join-Path $FRONTEND "package.json"))) {
    Write-Host "ERROR: frontend/package.json not found at $FRONTEND" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Frontend (Tunnel Mode)" -ForegroundColor Cyan
Write-Host "Backend URL: $BackendUrl"
Write-Host ""
Write-Host "Vite starts on http://0.0.0.0:5173"
Write-Host "Run tunnel_frontend.ps1 in another terminal to get a public URL."
Write-Host "Press Ctrl+C to stop."
Write-Host ""

$env:VITE_API_BASE_URL = $BackendUrl
Set-Location $FRONTEND
npm run dev -- --host 0.0.0.0
