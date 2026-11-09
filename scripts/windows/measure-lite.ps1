# Measures MetaSearch Lite on the machine it runs on (WP-14, checklist C11-C13). ASCII only.
#
#   measure-lite.ps1 idle     [-Port 8000] [-Minutes 10]   RSS and CPU while idle (C13)
#   measure-lite.ps1 restart  [-Port 8000]                 kill the process, time until the
#                                                          watchdog brings /readyz back (C12)
#   measure-lite.ps1 wake     [-Port 8000]                 run after each wake: time until
#                                                          /readyz answers (C11, repeat 5 times)
#
# Every result is appended to api\.lite.measure.log; send that file with the ticket.
param(
    [Parameter(Position = 0)]
    [ValidateSet("idle", "restart", "wake")]
    [string]$Mode = "idle",
    [int]$Port = 8000,
    [int]$Minutes = 10
)

$ErrorActionPreference = "Stop"
$Root = Split-Path (Split-Path $PSScriptRoot)
$ApiDir = Join-Path $Root "api"
$PidFile = Join-Path $env:TEMP "metasearch-lite-$Port.pid"
$Out = Join-Path $ApiDir ".lite.measure.log"

function Write-Result([string]$Line) {
    $stamped = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Line
    Add-Content -Path $Out -Value $stamped
    Write-Host $stamped
}

function Wait-Ready([int]$LimitSec) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $LimitSec) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/readyz" -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { return $sw.Elapsed.TotalSeconds }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    return -1
}

# The PID file holds the venv launcher (about 5 MB, idle); the server is the python.exe it
# starts. Measure and kill the server itself, or the numbers describe the launcher.
function Get-ServerProcess([int]$LauncherId) {
    $child = Get-CimInstance Win32_Process -Filter "ParentProcessId=$LauncherId" |
        Where-Object { $_.Name -eq "python.exe" } | Select-Object -First 1
    if ($child) { return Get-Process -Id $child.ProcessId }
    return Get-Process -Id $LauncherId
}

$procId = [int](Get-Content $PidFile | Select-Object -First 1)
$server = Get-ServerProcess $procId
Write-Host ("measuring server PID {0} (launcher {1})" -f $server.Id, $procId)

switch ($Mode) {
    "idle" {
        $proc = $server
        $cpu0 = $proc.TotalProcessorTime.TotalSeconds
        $peak = 0
        for ($i = 0; $i -lt ($Minutes * 6); $i++) {
            Start-Sleep -Seconds 10
            $proc.Refresh()
            if ($proc.WorkingSet64 -gt $peak) { $peak = $proc.WorkingSet64 }
        }
        $cpu = ($proc.TotalProcessorTime.TotalSeconds - $cpu0) / ($Minutes * 60) / [Environment]::ProcessorCount * 100
        Write-Result ("idle {0} min: peak RSS {1:N0} MB, CPU {2:N2}% of the machine" -f $Minutes, ($peak / 1MB), $cpu)
    }
    "restart" {
        Stop-Process -Id $server.Id -Force
        # Wait until it is really down, so the time below is the restart, not the old process.
        $down = [Diagnostics.Stopwatch]::StartNew()
        while ($down.Elapsed.TotalSeconds -lt 30) {
            try {
                Invoke-WebRequest -Uri "http://127.0.0.1:$Port/readyz" -TimeoutSec 2 -UseBasicParsing | Out-Null
                Start-Sleep -Milliseconds 300
            } catch { break }
        }
        $sec = Wait-Ready 600
        Write-Result ("restart after kill: /readyz back in {0:N1} s (-1 = not within 10 min)" -f $sec)
    }
    "wake" {
        $sec = Wait-Ready 120
        Write-Result ("after wake: /readyz in {0:N1} s (-1 = not within 2 min)" -f $sec)
    }
}
