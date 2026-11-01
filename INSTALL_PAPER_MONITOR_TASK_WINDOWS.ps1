param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$name = 'DayTraderIndiaPaperMonitor'
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
    Write-Host "Removed $name."
    exit 0
}
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python -ErrorAction Stop).Source
$action = New-ScheduledTaskAction -Execute $python -WorkingDirectory $project -Argument '-m india_trader paper-monitor watch --watchlist paper_watchlist.txt'
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '09:00'
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 8) -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Read-only Indian paper alerts, news, earnings and insider checks; no orders' -Force | Out-Null
Write-Host "Installed ${name}: weekdays 09:00 IST while you are signed in."
Write-Host "Events: $project\trader\state\paper-monitor-events.jsonl"
Write-Host "Remove with: powershell -File .\INSTALL_PAPER_MONITOR_TASK_WINDOWS.ps1 -Remove"
