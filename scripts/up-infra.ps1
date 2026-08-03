# up-infra.ps1 — bring up the INDEPENDENT infra stack (Postgres + Redis + MinIO)
# and leave it running. Idempotent: safe to re-run. Data persists in the
# external volumes (cashing-backend_pgdata / cashing-backend_miniodata).
#
# Usage (from your terminal):  .\scripts\up-infra.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot   # cashing-backend/
Set-Location $root

# Shared network both stacks join (create only if missing — avoids a fatal
# "already exists" under $ErrorActionPreference='Stop').
if (-not (docker network ls --filter "name=^cashing-net$" --format "{{.Name}}")) {
  docker network create cashing-net | Out-Null
}

Write-Host "Starting infra stack (cashing-infra): Postgres + Redis + MinIO..." -ForegroundColor Cyan
docker compose -f docker-compose.infra.yml up -d

Write-Host "Waiting for Postgres to report healthy..." -ForegroundColor Cyan
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
  $h = docker inspect --format '{{.State.Health.Status}}' cashing-infra-db-1 2>$null
  if ($h -eq "healthy") { $healthy = $true; break }
  Start-Sleep -Seconds 2
}
if ($healthy) { Write-Host "Infra ready. Postgres healthy on :5432." -ForegroundColor Green }
else { Write-Host "WARNING: Postgres did not report healthy within ~60s. Check 'docker compose -f docker-compose.infra.yml logs db'." -ForegroundColor Yellow }
