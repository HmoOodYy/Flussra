# Dev Tunnel - Cloudflare Quick Tunnel Setup

Share the app with testers remotely using Cloudflare Quick Tunnels.
No Cloudflare account required. Tunnels are temporary and free.

This is for development only.
Quick Tunnel URLs are public but ephemeral - they expire when you stop the process.
Never use this setup for real payroll data or production traffic.

---

## Prerequisites

cloudflared installed. Download from:
https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

Resolution order used by all scripts:
1. $env:CLOUDFLARED_PATH  (set this if cloudflared is installed somewhere unusual)
2. G:\Tools\cloudflared\cloudflared.exe
3. cloudflared found on $PATH

---

## Quick start (one command)

From the project root in PowerShell:

    .\open

or:

    .\open.ps1

This does everything automatically:
- Starts the backend (new window)
- Starts a Cloudflare tunnel for the backend
- Starts the frontend with VITE_API_BASE_URL pointing at the backend tunnel (new window)
- Starts a Cloudflare tunnel for the frontend
- Prints the public URL and copies it to the clipboard

Logs are written to .dev-tunnel\ in the project root.

Close the backend and frontend windows when you are done testing.

---

## Manual four-terminal flow

If you prefer to control each step yourself:

Terminal 1 - Backend
    .\scripts\dev_backend.ps1

Terminal 2 - Backend tunnel
    .\scripts\tunnel_backend.ps1
    (copy the https://*.trycloudflare.com URL that appears)

Terminal 3 - Frontend (paste backend tunnel URL)
    .\scripts\dev_frontend_tunnel.ps1 -BackendUrl "https://BACKEND.trycloudflare.com"

Terminal 4 - Frontend tunnel
    .\scripts\tunnel_frontend.ps1
    (send the printed URL to your testers)

---

## How it works

Layer               | Mechanism
--------------------|----------------------------------------------------------
Vite allowedHosts   | Set to true in vite.config.ts so any hostname is accepted
Backend CORS        | Regex ^https://[a-z0-9-]+\.trycloudflare\.com$ in dev only
VITE_API_BASE_URL   | Set per-session by open.ps1 or dev_frontend_tunnel.ps1

Both allowedHosts: true and the CORS regex are development-only.
Production behavior is unchanged.

---

## Logs

open.ps1 writes these files to .dev-tunnel\:

  backend-out.log       cloudflared stdout for backend tunnel
  backend-err.log       cloudflared stderr for backend tunnel (URL appears here)
  frontend-out.log      cloudflared stdout for frontend tunnel
  frontend-err.log      cloudflared stderr for frontend tunnel (URL appears here)
  start-frontend.ps1    generated script used to start Vite with env var set

All log files are gitignored (*.log and .dev-tunnel/ in .gitignore).

---

## Tips

- Tunnel URLs change every session. Run .\open again to get new ones.
- Use test accounts only. Do not share admin credentials with external testers.
- Stop tunnels by closing the relevant windows or pressing Ctrl+C.
- Quick Tunnels are not suitable for demos requiring stable URLs.
  Use a named Cloudflare Tunnel for that.

---

## Normal local dev (no tunnel)

No script needed. Just run:

    .\scripts\dev_backend.ps1

Then in another terminal:

    cd frontend
    npm run dev

VITE_API_BASE_URL defaults to whatever is in frontend\.env.local.
