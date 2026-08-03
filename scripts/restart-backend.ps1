# restart-backend.ps1 — restart ONLY the backend (always on host port 8000),
# leaving the infra/Postgres stack untouched. The source tree is bind-mounted
# and Dockerfile.dev runs uvicorn --reload, so this picks up local code without
# a rebuild. Pass -Build to force an image rebuild (only needed when deps or the
# Dockerfile change).
#
# Usage:  .\scripts\restart-backend.ps1   [-Build]
param([switch]$Build)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot   # cashing-backend/
Set-Location $root

# Make sure the shared network exists (infra should already be up via up-infra.ps1).
if (-not (docker network ls --filter "name=^cashing-net$" --format "{{.Name}}")) {
  docker network create cashing-net | Out-Null
}
if (-not (docker ps --filter "name=cashing-infra-db-1" --filter "status=running" --format "{{.Names}}")) {
  Write-Host "NOTE: infra (cashing-infra-db-1) is not running. Run .\scripts\up-infra.ps1 first." -ForegroundColor Yellow
}

$buildArg = @(); if ($Build) { $buildArg = @("--build") }
Write-Host "Restarting backend (cashing-backend) on :8000..." -ForegroundColor Cyan
docker compose -f docker-compose.yml up -d --force-recreate @buildArg app

Write-Host "Waiting for backend /health on :8000..." -ForegroundColor Cyan
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  try {
    $r = Invoke-WebRequest -Uri "http://localhost:8000/health" -UseBasicParsing -TimeoutSec 3
    if ($r.StatusCode -eq 200) { $ok = $true; break }
  } catch { Start-Sleep -Seconds 2 }
}
if ($ok) { Write-Host "Backend healthy on http://localhost:8000." -ForegroundColor Green }
else { Write-Host "WARNING: backend /health not 200 within ~60s. Check 'docker compose -f docker-compose.yml logs app'." -ForegroundColor Yellow }
