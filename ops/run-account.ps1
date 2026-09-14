param([Parameter(Mandatory=$true)][ValidateSet('1','2','3','4','5')][string]$Account)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path $PSScriptRoot -Parent
Set-Location $Repo
$LogDir = Join-Path $Repo 'data/logs'
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir ("daily-{0}-{1}.log" -f $Account, (Get-Date -Format 'yyyyMMdd-HHmmss'))
& "$Repo/.venv/Scripts/python.exe" "$Repo/scripts/daily.py" --compte $Account *> $Log
exit $LASTEXITCODE
