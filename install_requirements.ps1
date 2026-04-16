param(
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$requirementsPath = Join-Path $scriptDir "requirements.txt"

if (-not (Test-Path $requirementsPath)) {
    throw "requirements.txt not found at $requirementsPath"
}

Write-Host "Using Python: $PythonExe"
& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -r $requirementsPath

Write-Host ""
Write-Host "Installed project requirements from $requirementsPath"
Write-Host "You can now run:"
Write-Host "python -m streamlit run financial_validator_mvp/streamlit_bank_pnl_app.py"
