<#
.SYNOPSIS
    Local pre-merge gate mirroring .github/workflows/ci.yml.

.DESCRIPTION
    Runs, in order:
      1. The exact CI pytest+coverage command (job "pytest (SQLite, coverage >= 70%)"):
           uv run python -m pytest --cov=app --cov-report=term-missing --cov-report=xml
         The 70% floor comes from [tool.coverage.report].fail_under in pyproject.toml
         (picked up automatically by pytest-cov/coverage - no --cov-fail-under flag
         needed), so a below-threshold run already makes pytest itself exit non-zero;
         this script also parses the printed TOTAL % independently and never trusts
         exit code alone.
      2. ruff check / ruff format --check / mypy (job "ruff + mypy" - `continue-on-error:
         true` in CI, so these are reported but never fail this gate, matching CI).
      3. Optionally (-IncludePg) the real-Postgres suite via scripts/test-postgres.sh
         (mirrors the nightly test-postgres-nightly job) - this one DOES fail the gate,
         since running it is an explicit, deliberate ask.

    Never prints "GATE OK" unless the pytest step both exited 0 AND its own summary
    line parsed cleanly with zero failed/error tests AND coverage >= 70%.

.PARAMETER SkipPg
    Skip the real-Postgres suite. Default: true (Postgres is slow; SQLite pytest is
    the blocking gate, matching CI's own split between the push/PR job and the
    non-blocking nightly job).

.PARAMETER IncludePg
    Also run the fixed scripts/test-postgres.sh end to end. Overrides -SkipPg.

.EXAMPLE
    .\scripts\pre-merge.ps1
.EXAMPLE
    .\scripts\pre-merge.ps1 -IncludePg
#>
[CmdletBinding()]
param(
    [switch]$SkipPg = $true,
    [switch]$IncludePg
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Fail-Gate {
    param([string]$Step, [string]$Detail)
    Write-Host $Detail -ForegroundColor Red
    Write-Host "GATE FAIL $Step"
    exit 1
}

# --- git state -------------------------------------------------------------
$shortSha = (git rev-parse --short HEAD).Trim()
$dirty = if ((git status --porcelain) -ne "") { "yes" } else { "no" }

# --- 1. pytest + coverage (mirrors ci.yml's "pytest" job exactly) ----------
Write-Host ">> running pytest + coverage (mirrors CI)..." -ForegroundColor Cyan
$pytestOutput = & uv run python -m pytest --cov=app --cov-report=term-missing --cov-report=xml 2>&1
$pytestExit = $LASTEXITCODE
$pytestOutput | ForEach-Object { Write-Host $_ }

# Robustly find the LAST pytest summary line (there can be several
# "===== ... =====" banners earlier, e.g. from a "warnings summary" section  - 
# always anchor on the final one). Pytest's summary line always contains at
# least one of these count words followed by "in <seconds>s".
$summaryLines = $pytestOutput | Where-Object {
    $_ -match '\b\d+\s+(passed|failed|error|errors|skipped|xfailed|xpassed)\b' -and $_ -match '\sin\s[\d.]+s\b'
}
$summaryLine = $summaryLines | Select-Object -Last 1

if (-not $summaryLine) {
    Fail-Gate "pytest" "Could not find a pytest summary line in the output - treating as a failed run (never trust a silent/ambiguous result)."
}

function Get-Count {
    param([string]$Line, [string]$Word)
    $m = [regex]::Match($Line, "(\d+)\s+$Word\b")
    if ($m.Success) { return [int]$m.Groups[1].Value }
    return 0
}

$passed = Get-Count $summaryLine "passed"
$failed = Get-Count $summaryLine "failed"
$errors = Get-Count $summaryLine "error(?:s)?"
$skipped = Get-Count $summaryLine "skipped"
$xfailed = Get-Count $summaryLine "xfailed"
$xpassed = Get-Count $summaryLine "xpassed"
$totalTests = $passed + $failed + $errors + $skipped + $xfailed + $xpassed

if ($pytestExit -ne 0 -or $failed -gt 0 -or $errors -gt 0) {
    Fail-Gate "pytest" "pytest reported failures/errors (exit=$pytestExit, failed=$failed, errors=$errors). Summary: $summaryLine"
}

# --- coverage % (from --cov-report=term-missing's TOTAL line) --------------
$totalLine = ($pytestOutput | Where-Object { $_ -match '^TOTAL\b' } | Select-Object -Last 1)
if (-not $totalLine) {
    Fail-Gate "coverage" "Could not find the coverage TOTAL line in pytest output - treating as a failed run."
}
$covMatch = [regex]::Match($totalLine, '(\d+)%\s*$')
if (-not $covMatch.Success) {
    Fail-Gate "coverage" "Could not parse a coverage percentage from: $totalLine"
}
$coverage = [int]$covMatch.Groups[1].Value

# Belt-and-suspenders: pyproject.toml's [tool.coverage.report].fail_under = 70
# already makes pytest itself exit non-zero below threshold (caught above), but
# never rely on exit code alone - re-check the parsed number directly too.
if ($coverage -lt 70) {
    Fail-Gate "coverage" "Coverage $coverage% is below the 70% floor (pyproject.toml [tool.coverage.report].fail_under)."
}

# --- 2. lint: ruff + mypy (mirrors ci.yml's "lint" job - non-blocking) -----
Write-Host ">> running ruff check (non-blocking, mirrors CI)..." -ForegroundColor Cyan
$ruffCheckOutput = & uv run ruff check . 2>&1
$ruffCheckOutput | ForEach-Object { Write-Host $_ }
$ruffCheckMatch = [regex]::Match(($ruffCheckOutput -join "`n"), 'Found (\d+) error')
$ruffCheckCount = if ($ruffCheckMatch.Success) { [int]$ruffCheckMatch.Groups[1].Value } else { 0 }

Write-Host ">> running ruff format --check (non-blocking)..." -ForegroundColor Cyan
$ruffFormatOutput = & uv run ruff format --check . 2>&1
$ruffFormatOutput | ForEach-Object { Write-Host $_ }
$ruffFormatMatch = [regex]::Match(($ruffFormatOutput -join "`n"), '(\d+) files? would be reformatted')
$ruffFormatCount = if ($ruffFormatMatch.Success) { [int]$ruffFormatMatch.Groups[1].Value } else { 0 }

$ruffTotal = $ruffCheckCount + $ruffFormatCount
$ruffReport = if ($ruffTotal -eq 0) { "clean" } else { "$ruffTotal" }

Write-Host ">> running mypy (non-blocking, mirrors CI)..." -ForegroundColor Cyan
$mypyOutput = & uv run mypy app 2>&1
$mypyOutput | ForEach-Object { Write-Host $_ }
$mypyMatch = [regex]::Match(($mypyOutput -join "`n"), 'Found (\d+) error')
$mypyCount = if ($mypyMatch.Success) { [int]$mypyMatch.Groups[1].Value } else { 0 }
$mypyReport = if ($mypyCount -eq 0) { "clean" } else { "$mypyCount" }

# --- 3. optional real-Postgres suite (blocking IF explicitly requested) ----
if ($IncludePg) {
    Write-Host ">> running real-Postgres suite (scripts/test-postgres.sh, -IncludePg)..." -ForegroundColor Cyan
    & bash scripts/test-postgres.sh -q
    if ($LASTEXITCODE -ne 0) {
        Fail-Gate "postgres" "scripts/test-postgres.sh failed (exit=$LASTEXITCODE)."
    }
} elseif (-not $SkipPg) {
    # -SkipPg:$false without -IncludePg - treat the same as -IncludePg (explicit intent to run it).
    Write-Host ">> running real-Postgres suite (scripts/test-postgres.sh, -SkipPg:`$false)..." -ForegroundColor Cyan
    & bash scripts/test-postgres.sh -q
    if ($LASTEXITCODE -ne 0) {
        Fail-Gate "postgres" "scripts/test-postgres.sh failed (exit=$LASTEXITCODE)."
    }
} else {
    Write-Host ">> skipping real-Postgres suite (default; pass -IncludePg to run it)" -ForegroundColor DarkGray
}

Write-Host "GATE OK $shortSha tests=$totalTests coverage=$coverage% ruff=$ruffReport mypy=$mypyReport dirty=$dirty"
exit 0
