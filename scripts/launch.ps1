# One-command Mythos launcher for Windows (PowerShell).
#
#   .\scripts\launch.ps1            # setup + doctor + web control panel
#   .\scripts\launch.ps1 -Offline   # in-memory backends (no docker)
param([switch]$Offline)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# 1. Virtualenv + install
if (-not (Test-Path ".venv")) {
    Write-Host "[launch] creating virtualenv..."
    python -m venv .venv
}
& ".venv\Scripts\Activate.ps1"
pip install -q -e ".[orchestration]"
if ($LASTEXITCODE -ne 0) { pip install -q -e "." }

# 2. Config
python main.py --init

# 3. Infrastructure
$serveArgs = @("--serve")
if ($Offline) {
    $serveArgs += @("--bus", "inmemory", "--matrix", "inmemory")
} elseif (Get-Command docker -ErrorAction SilentlyContinue) {
    Write-Host "[launch] starting RabbitMQ + Qdrant..."
    docker compose up -d
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[launch] docker compose failed - continuing; use -Offline for in-memory mode"
    }
} else {
    Write-Host "[launch] docker not found - using in-memory backends"
    $serveArgs += @("--bus", "inmemory", "--matrix", "inmemory")
}

# 4. Diagnose + launch the control panel
python main.py --doctor
Write-Host ""
python main.py @serveArgs
