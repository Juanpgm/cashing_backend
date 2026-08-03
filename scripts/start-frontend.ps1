# start-frontend.ps1 — launch the Next.js frontend (native, port 3000) in its
# own window with a complete PATH (node/npm are not always on a non-interactive
# PATH). The frontend targets the backend via cashing-frontend/.env.local
# (NEXT_PUBLIC_API_URL=http://localhost:8000).
#
# Usage:  .\scripts\start-frontend.ps1
$ErrorActionPreference = "Stop"
$fe = (Resolve-Path (Join-Path $PSScriptRoot "..\..\cashing-frontend")).Path
$nodeDir = "C:\Program Files\nodejs"

Write-Host "Launching frontend (next dev) at $fe in a new window..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
  "-NoExit", "-Command",
  "`$env:Path = '$nodeDir;' + `$env:Path; Set-Location '$fe'; npm run dev"
)
Write-Host "Frontend starting on http://localhost:3000 (give it a few seconds)." -ForegroundColor Green
