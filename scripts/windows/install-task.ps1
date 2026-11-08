# Registers MetaSearch Lite with Task Scheduler (WP-14). PowerShell 5.1 or later, ASCII only.
#
#   install-task.ps1 [-Port 8000] [-EnvFile path]      register (run as the user who owns api\.venv)
#   install-task.ps1 -Remove                           unregister both tasks
#
# Two tasks:
#   MetaSearchLite          starts the service at logon; Task Scheduler restarts it on failure.
#   MetaSearchLiteWatchdog  every 5 minutes and on wake from sleep runs "metasearch-lite.ps1 check",
#                           which restarts the service when /readyz does not answer in 30 s.
param(
    [int]$Port = 8000,
    [string]$EnvFile = "",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$Manager = Join-Path $PSScriptRoot "metasearch-lite.ps1"
$Names = @("MetaSearchLite", "MetaSearchLiteWatchdog")

if ($Remove) {
    foreach ($n in $Names) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Host "removed $n"
        }
    }
    exit 0
}

$envArg = ""
if ($EnvFile) { $envArg = " -EnvFile `"$EnvFile`"" }
$base = "-NoProfile -ExecutionPolicy Bypass -File `"$Manager`""

$settings = New-ScheduledTaskSettingsSet -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)

$start = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "$base start -Port $Port$envArg"
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName "MetaSearchLite" -Action $start -Trigger $atLogon -Settings $settings -Force | Out-Null
Write-Host "registered MetaSearchLite (at logon, restart on failure)"

$check = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "$base check -Port $Port$envArg"
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
# Wake from sleep: Power-Troubleshooter event 1 in the System log.
$wakeClass = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$onWake = New-CimInstance -CimClass $wakeClass -ClientOnly
$onWake.Enabled = $true
$onWake.Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name=''Microsoft-Windows-Power-Troubleshooter''] and EventID=1]]</Select></Query></QueryList>'
Register-ScheduledTask -TaskName "MetaSearchLiteWatchdog" -Action $check -Trigger @($every5, $onWake) -Settings $settings -Force | Out-Null
Write-Host "registered MetaSearchLiteWatchdog (every 5 min and on wake)"
