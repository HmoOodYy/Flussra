# tunnel_backend.ps1
# Expose the local backend (http://localhost:8000) via Cloudflare Quick Tunnel.
#
# Usage:
#   .\scripts\tunnel_backend.ps1
#
# The tunnel URL printed to the console is temporary. Copy it and pass it
# to dev_frontend_tunnel.ps1 as the -BackendUrl argument.
#
# cloudflared resolution order:
#   1. $env:CLOUDFLARED_PATH  (set in your shell profile if cloudflared is elsewhere)
#   2. G:\Tools\cloudflared\cloudflared.exe
#   3. cloudflared from PATH

$ErrorActionPreference = "Stop"

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

$cloudflared = Find-Cloudflared

if (-not $cloudflared) {
    Write-Host ""
    Write-Host "ERROR: cloudflared not found." -ForegroundColor Red
    Write-Host "Install from: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    Write-Host 'Or set $env:CLOUDFLARED_PATH to its full path.'
    exit 1
}

Write-Host ""
Write-Host "Backend Tunnel - localhost:8000" -ForegroundColor Cyan
Write-Host "Using: $cloudflared"
Write-Host ""
Write-Host "Copy the https://*.trycloudflare.com URL, then run:"
Write-Host '  .\scripts\dev_frontend_tunnel.ps1 -BackendUrl "https://YOUR-BACKEND.trycloudflare.com"'
Write-Host ""
Write-Host "Press Ctrl+C to close the tunnel."
Write-Host ""

& $cloudflared tunnel --url http://localhost:8000
