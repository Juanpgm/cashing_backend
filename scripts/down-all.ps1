# down-all.ps1 — stop BOTH stacks (backend + infra). Volumes/data are preserved
# (no -v). Use this for a full clean stop; use up-infra.ps1 + restart-backend.ps1
# to bring things back.
#
# Usage:  .\scripts\down-all.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot   # cashing-backend/
Set-Location $root

Write-Host "Stopping backend stack (cashing-backend)..." -ForegroundColor Cyan
docker compose -f docker-compose.yml down

Write-Host "Stopping infra stack (cashing-infra)..." -ForegroundColor Cyan
docker compose -f docker-compose.infra.yml down

Write-Host "Both stacks stopped. Data volumes preserved." -ForegroundColor Green
