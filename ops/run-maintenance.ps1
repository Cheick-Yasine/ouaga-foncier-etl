$ErrorActionPreference = 'Stop'
$Repo = Split-Path $PSScriptRoot -Parent
Set-Location $Repo
$LogDir = Join-Path $Repo 'data/logs'
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir ("maintenance-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
& "$Repo/.venv/Scripts/python.exe" "$Repo/scripts/maintenance.py" *> $Log
exit $LASTEXITCODE
