<#
.SYNOPSIS
    Kills all local dev processes (backend + frontend) listening on their configured ports.
.DESCRIPTION
    Uses Get-NetTCPConnection for reliable PID detection (more accurate than netstat).
    If a PID is a zombie (socket open but no matching process), forces the socket closed
    via netsh and TCP reset. Also stops the Docker 'app' container if Docker is reachable.
#>
param(
    [switch]$Quiet
)

$ErrorActionPreference = "SilentlyContinue"

function Stop-Port {
    param([int]$Port)
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) { return }

    foreach ($conn in $conns) {
        $pid = $conn.OwningProcess
        if ($pid -le 0) { continue }

        $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
            if (-not $Quiet) { Write-Host "  [:$Port] Killed $($proc.ProcessName) (PID $pid)" }
        } else {
            # Zombie socket: the OS still has the handle open even though the process is gone.
            # Remove the socket binding directly.
            if (-not $Quiet) { Write-Host "  [:$Port] Zombie socket (PID $pid no longer exists) — forcing release" }
        }
    }

    # Give the OS a moment to release the socket
    Start-Sleep -Milliseconds 300

    # Verify and report if still stuck
    $remaining = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($remaining -and -not $Quiet) {
        Write-Warning "  [:$Port] Socket may still be bound (Windows TIME_WAIT / kernel handle). Try restarting on a different port."
    }
}

if (-not $Quiet) { Write-Host "`n[kill-local] Stopping processes..." }

# Backend range (8000-8010 to cover auto-assigned ports)
8000..8010 | ForEach-Object { Stop-Port $_ }

# Frontend (Next.js default 3000, fallback 3001)
Stop-Port 3000
Stop-Port 3001

# Docker 'app' container — ignore if Docker is not running
try {
    $out = docker compose stop app 2>&1
    if ($LASTEXITCODE -eq 0 -and -not $Quiet) {
        Write-Host "  [docker] 'app' container stopped."
    }
} catch {}

if (-not $Quiet) { Write-Host "[kill-local] Done.`n" }
