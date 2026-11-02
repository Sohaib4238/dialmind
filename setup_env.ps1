# Run this in every new PowerShell terminal before working on the project:
#   . .\setup_env.ps1
$env:NEXT_TELEMETRY_DISABLED = "1"
$env:NO_UPDATE_NOTIFIER = "1"

$Activate = Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1"
if (Test-Path $Activate) {
    . $Activate
} else {
    Write-Warning "No venv at .venv. Create one: python -m venv .venv; then pip install -r dashboard\backend\requirements.txt"
}
