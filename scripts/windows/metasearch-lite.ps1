# MetaSearch Lite service manager for Windows (WP-14). PowerShell 5.1 or later, ASCII only.
#
#   metasearch-lite.ps1 start   [-Port 8000] [-EnvFile path]   start in the background
#   metasearch-lite.ps1 stop                                   stop it
#   metasearch-lite.ps1 restart [-Port 8000] [-EnvFile path]
#   metasearch-lite.ps1 status  [-Port 8000]                   process and /readyz
#   metasearch-lite.ps1 check   [-Port 8000] [-EnvFile path]   self-check: restart when /readyz
#                                                              does not answer in time
#   metasearch-lite.ps1 open    [-Port 8000]                   open the Web UI in the browser
#
# Settings come from api\.env (copy deploy\.env.lite.example), or from -EnvFile.
# The server binds 127.0.0.1 only, with auth on. It refuses to start on a port that another
# process already holds, so it can never take over someone else's service.
#
# v3.1: the Web UI (frontend\out, built once with `npm run build:export`) is served by the
# API itself on the same port - no second process, no second terminal. The scheduled
# watchdog runs this script hidden and a per-port mutex makes overlapping invocations
# (5-min tick + wake event) skip instead of piling up new console windows.
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "check", "open")]
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

# v3.1 single-run guard: a named per-port mutex. The watchdog fires every 5 min AND on
# wake; if another manager invocation is already active, this one exits quietly instead
# of stacking another run (and another console window) on top.
$ManagerMutex = New-Object System.Threading.Mutex($false, "Local\MetaSearchLiteManager-$Port")
if (-not $ManagerMutex.WaitOne(0)) {
    Write-Host "another metasearch-lite manager run is active on port $Port; skipping this invocation"
    exit 0
}

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

function Test-LiteCommandLine([int]$ProcId) {
    # A PID alone is not proof: after a reboot or a crash Windows reuses PIDs, and another
    # python process on this machine (a different app, a test run) can get the one in the PID
    # file. Only a process started as this server on this port counts; anything else, or a
    # command line we cannot read, is not ours and must never be stopped by this script.
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcId" -ErrorAction SilentlyContinue
    if (-not $cim -or -not $cim.CommandLine) { return $false }
    $cmd = $cim.CommandLine
    return ($cmd -like "*uvicorn*app.main:app*") -and ($cmd -match "--port\s+$Port(\s|$)")
}

function Get-LiteProcess {
    if (-not (Test-Path $PidFile)) { return $null }
    $procId = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $procId) { return $null }
    $proc = Get-Process -Id ([int]$procId) -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }
    if (Test-LiteCommandLine $proc.Id) { return $proc }
    # Stale PID file pointing at someone else's process: forget it, leave that process alone.
    Write-Log "PID file names PID $($proc.Id) ($($proc.ProcessName)), which is not MetaSearch Lite on port $Port; ignoring it"
    Remove-Item $PidFile -ErrorAction SilentlyContinue
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
    # The app reads the file itself (METASEARCH_ENV_FILE, after api\.env), with the same
    # dotenv parser as api\.env. Parsing it here went wrong twice: inline comments became
    # values, and an empty value deleted the variable, so the built-in default came back.
    if (-not $Path) { return }
    if (-not (Test-Path $Path)) { throw "env file not found: $Path" }
    $env:METASEARCH_ENV_FILE = (Resolve-Path $Path).Path
}

function Ensure-LiteUi {
    # v3.1: build the Web UI static export ONCE (frontend\out is gitignored). The API
    # serves it on the same port, so Lite needs no separate UI process/terminal.
    $uiIndex = Join-Path $Root "frontend\out\index.html"
    if (Test-Path $uiIndex) { return }
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) {
        Write-Log "Web UI not built (frontend\out missing) and npm not found; build once: cd frontend; npm ci; npm run build:export"
        return
    }
    $uiLog = Join-Path $ApiDir ".lite.ui-build.log"
    Write-Log "building Web UI once into frontend\out (log: $uiLog) ..."
    $frontendDir = Join-Path $Root "frontend"
    if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
        & npm --prefix $frontendDir ci *> $uiLog
    }
    & npm --prefix $frontendDir run build:export *> $uiLog
    if (Test-Path $uiIndex) {
        Write-Log "Web UI ready -> http://127.0.0.1:$Port/"
    } else {
        Write-Log "Web UI build failed (see $uiLog); API-only mode still works on port $Port"
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
    Ensure-LiteUi
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
        # Run by the scheduled watchdog (hidden window), also right after the machine
        # wakes up. The per-port mutex above already dropped overlapping invocations.
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
    "open" {
        # Open the Web UI (served by the API on the same port) in the default browser.
        if (-not (Get-LiteProcess)) { Write-Host "not running"; exit 3 }
        Start-Process "http://127.0.0.1:$Port/"
        Write-Host "opened http://127.0.0.1:$Port/"
    }
}
