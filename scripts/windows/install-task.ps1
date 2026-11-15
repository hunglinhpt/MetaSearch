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
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [string]$EnvFile = "",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$Manager = Join-Path $PSScriptRoot "metasearch-lite.ps1"
if ($Manager -match '["\r\n]' -or $Manager.EndsWith('\')) {
    throw "Unsafe path for manager script: $Manager"
}
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
if ($EnvFile) {
    if ($EnvFile -match '["\r\n]' -or $EnvFile.EndsWith('\')) {
        throw "Unsafe characters in EnvFile path"
    }
    $resolvedEnv = (Resolve-Path -LiteralPath $EnvFile).Path
    if ($resolvedEnv -match '["\r\n]' -or $resolvedEnv.EndsWith('\')) {
        throw "Unsafe characters in resolved EnvFile path"
    }
    $envArg = " -EnvFile `"$resolvedEnv`""
}
# -WindowStyle Hidden: the watchdog fires every 5 min and on every wake -
# without this each firing pops a console window into the interactive session.
$base = "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"$Manager`""

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
