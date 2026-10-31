# Registers Windows scheduled tasks: the full run daily or every few hours ("JustJob"), and a quick
# refresh of companies with recent matches every hour ("JustJob-hot").
# Usage (from the project folder):  powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -At 9am
#                  every 4 hours:   powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -At 9am -EveryHours 4
#    without the hourly refresh:    add -HotEveryHours 0
# Remove them again with:           Unregister-ScheduledTask -TaskName JustJob; Unregister-ScheduledTask -TaskName JustJob-hot
param([string]$At = "9am", [int]$EveryHours = 0, [int]$HotEveryHours = 1)

$project = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python -ErrorAction Stop).Source
# pythonw runs without a console window: no black window popping up every hour, and nothing to
# close by accident (closing that window kills the run). Output goes to data\logs\justjob.log.
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (Test-Path $pythonw) { $python = $pythonw }

# StartWhenAvailable: if the PC was off at the scheduled time, run as soon as it's back on.
# On a laptop, Windows' defaults would skip runs on battery and kill a run when it's unplugged.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$action = New-ScheduledTaskAction -Execute $python -Argument "-m justjob run" -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Daily -At $At
if ($EveryHours -gt 0) {
    $trigger = New-ScheduledTaskTrigger -Once -At $At -RepetitionInterval (New-TimeSpan -Hours $EveryHours)
}
Register-ScheduledTask -TaskName "JustJob" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Job fetch, filter and digest (just-job)" -Force | Out-Null
if ($EveryHours -gt 0) { Write-Host "Scheduled 'JustJob' every $EveryHours hours from $At using $python" }
else { Write-Host "Scheduled 'JustJob' daily at $At using $python" }

if ($HotEveryHours -gt 0) {
    # Half an hour off the full run's schedule; if they still meet, the refresh skips its turn.
    $hotAction = New-ScheduledTaskAction -Execute $python -Argument "-m justjob run --hot" -WorkingDirectory $project
    $hotTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date $At).AddMinutes(30)) `
        -RepetitionInterval (New-TimeSpan -Hours $HotEveryHours)
    Register-ScheduledTask -TaskName "JustJob-hot" -Action $hotAction -Trigger $hotTrigger -Settings $settings `
        -Description "Hourly refresh of companies with recent matches (just-job)" -Force | Out-Null
    Write-Host "Scheduled 'JustJob-hot' every $HotEveryHours hour(s) from $((Get-Date $At).AddMinutes(30).ToShortTimeString())"
}
else {
    Unregister-ScheduledTask -TaskName "JustJob-hot" -Confirm:$false -ErrorAction SilentlyContinue
}
