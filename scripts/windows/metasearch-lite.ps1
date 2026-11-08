# MetaSearch Lite service manager for Windows (WP-14). PowerShell 5.1 or later, ASCII only.
#
#   metasearch-lite.ps1 start   [-Port 8000] [-EnvFile path]   start in the background
#   metasearch-lite.ps1 stop                                   stop it
#   metasearch-lite.ps1 restart [-Port 8000] [-EnvFile path]
#   metasearch-lite.ps1 status  [-Port 8000]                   process and /readyz
#   metasearch-lite.ps1 check   [-Port 8000] [-EnvFile path]   self-check: restart when /readyz
#                                                              does not answer in time
#
# Settings come from api\.env (copy deploy\.env.lite.example), or from -EnvFile.
# The server binds 127.0.0.1 only, with auth on. It refuses to start on a port that another
# process already holds, so it can never take over someone else's service.
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "check")]
    [string]$Action = "status",
    [int]$Port = 8000,
    [string]$EnvFile = "",
    [int]$ReadyTimeoutSec = 30,
    [long]$MaxLogBytes = 10MB
)

$ErrorActionPreference = "Stop"
$Root = Split-Path (Split-Path $PSScriptRoot)
$ApiDir = Join-Path $Root "api"
$Python = Join-Path $ApiDir ".venv\Scripts\python.exe"
$PidFile = Join-Path $env:TEMP "metasearch-lite-$Port.pid"
$LogFile = Join-Path $ApiDir ".lite.log"
$ErrFile = Join-Path $ApiDir ".lite.err.log"

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path (Join-Path $ApiDir ".lite.manager.log") -Value $line
    Write-Host $Message
}

function Invoke-LogRotation {
    # Keep each log under MaxLogBytes: the current file moves to .1, the old .1 is dropped.
    foreach ($log in @($LogFile, $ErrFile)) {
        if ((Test-Path $log) -and ((Get-Item $log).Length -gt $MaxLogBytes)) {
            Move-Item -Path $log -Destination "$log.1" -Force
        }
    }
}

function Get-LiteProcess {
    if (-not (Test-Path $PidFile)) { return $null }
    $procId = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $procId) { return $null }
    $proc = Get-Process -Id ([int]$procId) -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -match "python") { return $proc }
    return $null
}

function Test-PortBusy([int]$P) {
    # Something already accepts connections on this port: never start on top of it.
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $client.BeginConnect("127.0.0.1", $P, $null, $null)
        if ($ar.AsyncWaitHandle.WaitOne(500) -and $client.Connected) { return $true }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Test-Ready([int]$TimeoutSec) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/readyz" -TimeoutSec $TimeoutSec -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Import-EnvFile([string]$Path) {
    if (-not $Path) { return }
    if (-not (Test-Path $Path)) { throw "env file not found: $Path" }
    foreach ($line in Get-Content $Path) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
        $k, $v = $t.Split("=", 2)
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim().Trim('"'), "Process")
    }
}

function Start-Lite {
    $existing = Get-LiteProcess
    if ($existing) { Write-Log "already running (PID $($existing.Id))"; return }
    if (Test-PortBusy $Port) {
        Write-Log "port $Port is held by another process; not starting"
        exit 2
    }
    if (-not (Test-Path $Python)) {
        Write-Log "venv not found at $Python (cd api; python -m venv .venv; .venv\Scripts\pip install -e .)"
        exit 1
    }
    Import-EnvFile $EnvFile
    Invoke-LogRotation
    $env:API_HOST = "127.0.0.1"
    $env:AUTH_ENABLED = "true"
    $proc = Start-Process -FilePath $Python `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port", "--timeout-graceful-shutdown", "15" `
        -WorkingDirectory $ApiDir -WindowStyle Hidden `
        -RedirectStandardOutput $LogFile -RedirectStandardError $ErrFile -PassThru
    Set-Content -Path $PidFile -Value $proc.Id
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Ready 3) { Write-Log "started (PID $($proc.Id)) on 127.0.0.1:$Port"; return }
        Start-Sleep -Seconds 1
    }
    Write-Log "started (PID $($proc.Id)) but /readyz is not 200 yet; see $ErrFile"
}

function Stop-Lite {
    $proc = Get-LiteProcess
    if ($proc) {
        # taskkill /T also ends the child the venv launcher starts
        & taskkill /PID $proc.Id /T /F | Out-Null
        Write-Log "stopped (PID $($proc.Id))"
    } else {
        Write-Log "not running"
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}

switch ($Action) {
    "start" { Start-Lite }
    "stop" { Stop-Lite }
    "restart" { Stop-Lite; Start-Sleep -Seconds 2; Start-Lite }
    "status" {
        $proc = Get-LiteProcess
        if (-not $proc) { Write-Host "not running"; exit 3 }
        if (Test-Ready 5) { Write-Host "running (PID $($proc.Id)), /readyz 200" }
        else { Write-Host "running (PID $($proc.Id)), /readyz not ready"; exit 4 }
    }
    "check" {
        # Run by the scheduled watchdog, also right after the machine wakes up.
        Invoke-LogRotation
        $proc = Get-LiteProcess
        if (-not $proc) { Write-Log "check: not running, starting"; Start-Lite; return }
        if (-not (Test-Ready $ReadyTimeoutSec)) {
            Write-Log "check: /readyz did not answer within $ReadyTimeoutSec s, restarting"
            Stop-Lite
            Start-Sleep -Seconds 2
            Start-Lite
        }
    }
}
