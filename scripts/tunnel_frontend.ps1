# tunnel_frontend.ps1
# Expose the local Vite dev server (http://localhost:5173) via Cloudflare Quick Tunnel.
# Send the printed URL to your testers.
#
# Usage:
#   .\scripts\tunnel_frontend.ps1
#
# cloudflared resolution order:
#   1. $env:CLOUDFLARED_PATH
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
Write-Host "Frontend Tunnel - localhost:5173" -ForegroundColor Cyan
Write-Host "Using: $cloudflared"
Write-Host ""
Write-Host "Share the https://*.trycloudflare.com URL with your testers."
Write-Host ""
Write-Host "Press Ctrl+C to close the tunnel."
Write-Host ""

& $cloudflared tunnel --url http://localhost:5173
