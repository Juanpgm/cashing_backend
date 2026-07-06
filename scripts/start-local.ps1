<#
.SYNOPSIS
    Starts the full local dev environment WITHOUT Docker.
.DESCRIPTION
    1. Kills any existing processes on backend/frontend ports.
    2. Finds a free port starting from $BackendPort (default 8000).
       If the preferred port has a zombie socket (Windows keeps the handle
       after a process dies), it automatically moves to the next free port.
    3. Starts the backend with SQLite (no Postgres, no Docker needed).
       Alembic will warn and fail gracefully on SQLite; create_all handles schema.
    4. Writes NEXT_PUBLIC_API_URL=http://localhost:<port> to the frontend .env.local.
    5. Starts the Next.js frontend.

    Each service opens in its own PowerShell window so you can see their logs.
    To stop everything: .\scripts\kill-local.ps1

.PARAMETER BackendPort
    First port to try for the backend (default 8000). Auto-increments if occupied.
.PARAMETER FrontendPort
    Port for Next.js (default 3000).
.PARAMETER NoFrontend
    Skip starting the frontend (backend only).

.EXAMPLE
    .\scripts\start-local.ps1
    .\scripts\start-local.ps1 -BackendPort 8001 -NoFrontend
#>
param(
    [int]$BackendPort  = 8000,
    [int]$FrontendPort = 3000,
    [switch]$NoFrontend
)

$ErrorActionPreference = "SilentlyContinue"

# ── paths ──────────────────────────────────────────────────────────────────

$backendDir  = Split-Path $PSScriptRoot -Parent
$repoRoot    = Split-Path $backendDir   -Parent
$frontendDir = Join-Path $repoRoot "cashing-frontend"

# ── helpers ────────────────────────────────────────────────────────────────

function Test-PortFree {
    param([int]$Port)
    # Get-NetTCPConnection is more reliable than netstat for PID detection
    $used = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return (-not $used)
}

function Find-FreePort {
    param([int]$Start)
    for ($p = $Start; $p -le ($Start + 20); $p++) {
        if (Test-PortFree $p) { return $p }
    }
    throw "No free port found in range $Start-$($Start + 20). Run .\scripts\kill-local.ps1 first."
}

function Stop-Port {
    param([int]$Port)
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) { return }
    foreach ($conn in $conns) {
        $p = $conn.OwningProcess
        if ($p -gt 0) {
            $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
            if ($proc) {
                Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
                Write-Host "  [:$Port] Killed $($proc.ProcessName) (PID $p)"
            } else {
                Write-Host "  [:$Port] Zombie socket on PID $p — will try next free port"
            }
        }
    }
}

# ── kill existing dev processes ────────────────────────────────────────────

Write-Host "`n[start-local] Cleaning up ports..."
8000..8010 | ForEach-Object { Stop-Port $_ }
Stop-Port 3000
Stop-Port 3001

# Stop Docker app container (keep infra running if Docker is up — db/redis/minio untouched)
try { docker compose --project-directory $backendDir stop app 2>$null | Out-Null } catch {}

Start-Sleep -Milliseconds 600

# ── find free backend port ─────────────────────────────────────────────────

try {
    $BackendPort = Find-FreePort $BackendPort
} catch {
    Write-Error $_
    exit 1
}
Write-Host "[start-local] Backend port: $BackendPort"

# ── upsert NEXT_PUBLIC_API_URL in frontend .env.local ─────────────────────
# Preserves all other variables (Firebase keys, etc.) already in the file.

$frontendEnv = Join-Path $frontendDir ".env.local"
$apiLine = "NEXT_PUBLIC_API_URL=http://localhost:$BackendPort"

if (Test-Path $frontendEnv) {
    $lines   = Get-Content $frontendEnv
    $found   = $false
    $updated = $lines | ForEach-Object {
        if ($_ -match '^NEXT_PUBLIC_API_URL=') { $apiLine; $found = $true }
        else { $_ }
    }
    if (-not $found) { $updated += $apiLine }
    $updated | Set-Content -Path $frontendEnv -Encoding utf8
} else {
    $apiLine | Set-Content -Path $frontendEnv -Encoding utf8
}

Write-Host "[start-local] $frontendEnv -> NEXT_PUBLIC_API_URL=http://localhost:$BackendPort"

# ── launch backend in a new window ─────────────────────────────────────────
# SQLite: no Docker, no Postgres required.
# Alembic will fail silently (no 'alembic' in PATH, or SQLite dialect mismatch).
# The app lifespan falls back to create_all which creates all schema from models.

$backendCmd = @"
`$env:DATABASE_URL     = 'sqlite+aiosqlite:///./local_dev.db'
`$env:STORAGE_PROVIDER = 'local'
Set-Location '$backendDir'
Write-Host '=== Backend (SQLite) on port $BackendPort ===' -ForegroundColor Cyan
uv run uvicorn app.main:app --host 0.0.0.0 --port $BackendPort --reload
"@

Start-Process powershell -ArgumentList @("-NoExit", "-Command", $backendCmd)
Write-Host "[start-local] Backend window opened."

# ── launch frontend in a new window ────────────────────────────────────────

if (-not $NoFrontend) {
    if (-not (Test-Path $frontendDir)) {
        Write-Warning "Frontend directory not found: $frontendDir — skipping."
    } else {
        $frontendCmd = @"
Set-Location '$frontendDir'
Write-Host '=== Frontend on port $FrontendPort ===' -ForegroundColor Green
npm run dev -- --port $FrontendPort
"@
        Start-Process powershell -ArgumentList @("-NoExit", "-Command", $frontendCmd)
        Write-Host "[start-local] Frontend window opened."
    }
}

# ── summary ────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  Backend  -> http://localhost:$BackendPort" -ForegroundColor Cyan
Write-Host "  API docs -> http://localhost:$BackendPort/docs" -ForegroundColor Cyan
if (-not $NoFrontend) {
    Write-Host "  Frontend -> http://localhost:$FrontendPort" -ForegroundColor Green
}
Write-Host ""
Write-Host "To stop everything: .\scripts\kill-local.ps1" -ForegroundColor Yellow
Write-Host ""
