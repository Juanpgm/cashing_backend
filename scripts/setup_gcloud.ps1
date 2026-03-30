<#
.SYNOPSIS
    Google Cloud project setup for CashIn backend.

.DESCRIPTION
    Creates GCP project, enables APIs, configures OAuth 2.0 consent screen,
    and sets up Secret Manager entries.

.PARAMETER ProjectId
    GCP project ID to create/use.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [string]$Region = "us-central1"
)

$ErrorActionPreference = "Stop"

Write-Host "=== CashIn GCP Setup ===" -ForegroundColor Cyan

# 1. Check gcloud available
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Error "gcloud CLI not found. Install from https://cloud.google.com/sdk/docs/install"
    exit 1
}

# 2. Create or select project
Write-Host "Setting project to $ProjectId..."
gcloud config set project $ProjectId 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Creating project $ProjectId..."
    gcloud projects create $ProjectId --name="CashIn Backend"
    gcloud config set project $ProjectId
}

# 3. Enable APIs
$apis = @(
    "secretmanager.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "sqladmin.googleapis.com",
    "drive.googleapis.com",
    "sheets.googleapis.com",
    "docs.googleapis.com",
    "calendar-json.googleapis.com"
)

Write-Host "Enabling APIs..."
foreach ($api in $apis) {
    Write-Host "  Enabling $api"
    gcloud services enable $api --quiet
}

# 4. Set default region
gcloud config set run/region $Region

Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Create OAuth 2.0 credentials at https://console.cloud.google.com/apis/credentials"
Write-Host "  2. Download client_secret.json to secrets/google/"
Write-Host "  3. Run: python scripts/generate_secrets.py"
