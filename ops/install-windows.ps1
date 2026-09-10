# Exécuter depuis PowerShell administrateur après configuration .env et sessions.
# Le mot de passe Windows est saisi ici, jamais dans GitHub ni dans le chat.
$ErrorActionPreference = 'Stop'
$Repo = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path "$Repo/.venv/Scripts/python.exe")) { throw 'Créer .venv et installer requirements.txt au préalable.' }
$Credentials = Get-Credential -Message 'Compte Windows qui exécutera les tâches même session fermée'
foreach ($Account in 1..5) {
    $Arguments = '-NoProfile -NonInteractive -File "{0}" -Account {1}' -f "$Repo/ops/run-account.ps1", $Account
    $Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $Arguments -WorkingDirectory $Repo
    $Trigger = New-ScheduledTaskTrigger -Daily -At ("{0:00}:00" -f ($Account + 1))
    $Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 6) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName "Ouaga-ETL-$Account" -Action $Action -Trigger $Trigger -Settings $Settings -User $Credentials.UserName -Password ($Credentials.GetNetworkCredential().Password) -Description 'Collecte locale quotidienne et reprise des fichiers en attente' -Force | Out-Null
}
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -File "{0}"' -f "$Repo/ops/run-maintenance.ps1") -WorkingDirectory $Repo
$Trigger = New-ScheduledTaskTrigger -Daily -At '14:00'
Register-ScheduledTask -TaskName 'Ouaga-ETL-Maintenance' -Action $Action -Trigger $Trigger -Settings $Settings -User $Credentials.UserName -Password ($Credentials.GetNetworkCredential().Password) -Description 'Contrôle de fraîcheur et sauvegarde quotidienne' -Force | Out-Null
Write-Output 'Maintenance à 14h et cinq tâches de collecte créées (02h à 06h, heure Windows). Testez chacune dans le Planificateur.'
