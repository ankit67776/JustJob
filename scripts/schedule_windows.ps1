# Registers a Windows scheduled task that runs the pipeline daily, or every few hours.
# Usage (from the project folder):  powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -At 9am
#                  every 6 hours:   powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -At 9am -EveryHours 6
# Remove it again with:             Unregister-ScheduledTask -TaskName JustJob
param([string]$At = "9am", [int]$EveryHours = 0)

$project = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python -ErrorAction Stop).Source

$action = New-ScheduledTaskAction -Execute $python -Argument "-m justjob run" -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Daily -At $At
if ($EveryHours -gt 0) {
    $trigger = New-ScheduledTaskTrigger -Once -At $At -RepetitionInterval (New-TimeSpan -Hours $EveryHours)
}
# StartWhenAvailable: if the PC was off at the scheduled time, run as soon as it's back on.
# On a laptop, Windows' defaults would skip runs on battery and kill a run when it's unplugged.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "JustJob" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Job fetch, filter and digest (just-job)" -Force | Out-Null
if ($EveryHours -gt 0) { Write-Host "Scheduled 'JustJob' every $EveryHours hours from $At using $python" }
else { Write-Host "Scheduled 'JustJob' daily at $At using $python" }
