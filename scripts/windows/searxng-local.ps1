# Native local SearXNG for MetaSearch Lite on Windows (docs/36 S21). PowerShell 5.1+, ASCII only.
#
#   searxng-local.ps1 setup         [-Dir path] [-Port 8888] [-Ref <commit>]   install or update
#   searxng-local.ps1 start|stop|restart|status|check [-Dir path] [-Port 8888]
#   searxng-local.ps1 install-task  [-Dir path] [-Port 8888]   start at logon + check every 5 min
#   searxng-local.ps1 remove-task
#
# Why native: no Docker/WSL VM on the target machine; SearXNG itself is ~100 MB RAM and idle
# CPU. It binds 127.0.0.1 only, limiter off, JSON format on (deploy\searxng\settings.yml plus
# local overrides from searxng_local_settings.py). Point MetaSearch at it with
#   SEARXNG_ENABLED=true
#   SEARXNG_URL=http://127.0.0.1:8888
# MetaSearch degrades gracefully when SearXNG is down (the adapter fails, other engines answer).
param(
    [Parameter(Position = 0)]
    [ValidateSet("setup", "start", "stop", "restart", "status", "check", "install-task", "remove-task")]
    [string]$Action = "status",
    [string]$Dir = "",
    [ValidateRange(1024, 65535)]
    [int]$Port = 8888,
    # Pinned upstream commit (2026-11-26), verified with MetaSearch's JSON adapter.
    [string]$Ref = "b5605b8f2fa2591108ab8e3f4fc16e88f7d66fe4",
    [long]$MaxLogBytes = 10MB
)

$ErrorActionPreference = "Stop"
$Root = Split-Path (Split-Path $PSScriptRoot)
if (-not $Dir) { $Dir = Join-Path (Split-Path $Root) "searxng-local" }
if ($Dir -match '["\r\n]' -or $Dir.EndsWith('\')) { throw "Unsafe characters in Dir path" }
if ($Ref -notmatch '^[0-9a-f]{7,40}$') { throw "Ref must be a commit hash" }
$Src = Join-Path $Dir "src"
$VenvPy = Join-Path $Dir "venv\Scripts\python.exe"
$Settings = Join-Path $Dir "settings.yml"
$PidFile = Join-Path $Dir "searxng-$Port.pid"
$LogFile = Join-Path $Dir "searxng.log"
$ErrFile = Join-Path $Dir "searxng.err.log"
$Helper = Join-Path $PSScriptRoot "searxng_local_settings.py"
$TaskNames = @("MetaSearchSearXNG", "MetaSearchSearXNGWatchdog")

$Mutex = New-Object System.Threading.Mutex($false, "Local\MetaSearchSearXNG-$Port")
if (-not $Mutex.WaitOne(0)) {
    Write-Host "another searxng-local run is active on port $Port; skipping"
    exit 0
}

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    if (Test-Path $Dir) { Add-Content -Path (Join-Path $Dir "manager.log") -Value $line }
    Write-Host $Message
}

function Test-OurCommandLine([int]$ProcId) {
    # A PID alone is not proof (PIDs are reused): only a SearXNG webapp started from this
    # directory counts. Anything else is never stopped by this script.
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcId" -ErrorAction SilentlyContinue
    if (-not $cim -or -not $cim.CommandLine) { return $false }
    return ($cim.CommandLine -like "*searx.webapp*") -and ($cim.ExecutablePath -like "$Dir\*")
}

function Get-OurProcess {
    if (-not (Test-Path $PidFile)) { return $null }
    $procId = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $procId) { return $null }
    $proc = Get-Process -Id ([int]$procId) -ErrorAction SilentlyContinue
    if ($proc -and (Test-OurCommandLine $proc.Id)) { return $proc }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    return $null
}

function Test-PortBusy([int]$P) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $client.BeginConnect("127.0.0.1", $P, $null, $null)
        return ($ar.AsyncWaitHandle.WaitOne(500) -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Test-Healthy([int]$TimeoutSec) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec $TimeoutSec -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Invoke-Setup {
    if ($Ref.Length -ne 40) { throw "setup needs the full 40-hex commit id in -Ref" }
    if (Get-OurProcess) { throw "stop SearXNG before setup (searxng-local.ps1 stop)" }
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    if (-not (Test-Path $VenvPy)) {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) { & py -3.12 -m venv (Join-Path $Dir "venv") } else { & python -m venv (Join-Path $Dir "venv") }
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
    }
    # Pinned upstream archive, only searx\ + requirement files (see the helper: git checkout
    # fails on Windows on upstream's "...conf:socket" template names).
    $rev = Join-Path $Src "REVISION"
    $have = if (Test-Path $rev) { (Get-Content $rev | Select-Object -First 1).Trim() } else { "" }
    if ($have -ne $Ref) {
        & $VenvPy $Helper --dir $Dir --fetch $Ref
        if ($LASTEXITCODE -ne 0) { throw "source download failed" }
    }
    & $VenvPy -m pip install -q --upgrade pip
    & $VenvPy -m pip install -q -r (Join-Path $Src "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    & $VenvPy $Helper --dir $Dir --port $Port
    if ($LASTEXITCODE -ne 0) { throw "settings generation failed" }
    Write-Log "setup done: $Dir (searxng $Ref)"
}

function Invoke-LogRotation {
    foreach ($log in @($LogFile, $ErrFile)) {
        if ((Test-Path $log) -and ((Get-Item $log).Length -gt $MaxLogBytes)) {
            Move-Item -Path $log -Destination "$log.1" -Force
        }
    }
}

function Start-SearXNG {
    if (Get-OurProcess) { Write-Log "already running on 127.0.0.1:$Port"; return }
    if (-not (Test-Path $VenvPy) -or -not (Test-Path $Settings)) {
        throw "not set up: run 'searxng-local.ps1 setup' first"
    }
    if (Test-PortBusy $Port) { throw "port $Port is already in use by another process" }
    Invoke-LogRotation
    $env:SEARXNG_SETTINGS_PATH = $Settings
    $env:PYTHONIOENCODING = "utf-8"
    $proc = Start-Process -FilePath $VenvPy -ArgumentList @("-m", "searx.webapp") `
        -WorkingDirectory $Src -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $LogFile -RedirectStandardError $ErrFile
    Set-Content -Path $PidFile -Value $proc.Id
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Healthy 2) { Write-Log "started (PID $($proc.Id)) on 127.0.0.1:$Port"; return }
        if ($proc.HasExited) { break }
    }
    Write-Log "SearXNG did not become healthy; see $ErrFile"
    exit 1
}

function Stop-SearXNG {
    $proc = Get-OurProcess
    if (-not $proc) { Write-Log "not running"; return }
    # The venv python.exe is a launcher with a child interpreter: stop the whole tree.
    & taskkill.exe /PID $proc.Id /T /F | Out-Null
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Write-Log "stopped (PID $($proc.Id))"
}

switch ($Action) {
    "setup" { Invoke-Setup }
    "start" { Start-SearXNG }
    "stop" { Stop-SearXNG }
    "restart" { Stop-SearXNG; Start-Sleep -Seconds 1; Start-SearXNG }
    "status" {
        $proc = Get-OurProcess
        $state = if ($proc) { "running (PID $($proc.Id))" } else { "not running" }
        $health = if (Test-Healthy 3) { "healthy" } else { "no answer" }
        Write-Host "searxng-local: $state, /healthz $health, 127.0.0.1:$Port"
    }
    "check" {
        if (-not (Test-Healthy 10)) {
            Write-Log "health check failed; restarting"
            Stop-SearXNG
            Start-SearXNG
        }
    }
    "install-task" {
        $self = $MyInvocation.MyCommand.Path
        if ($self -match '["\r\n]' -or $self.EndsWith('\')) { throw "Unsafe script path" }
        $base = "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"$self`""
        $tail = " -Dir `"$Dir`" -Port $Port"
        $settings = New-ScheduledTaskSettingsSet -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
            -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 0)
        $start = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "$base start$tail"
        $atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        Register-ScheduledTask -TaskName $TaskNames[0] -Action $start -Trigger $atLogon -Settings $settings -Force | Out-Null
        $check = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "$base check$tail"
        $every5 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
        Register-ScheduledTask -TaskName $TaskNames[1] -Action $check -Trigger $every5 -Settings $settings -Force | Out-Null
        Write-Host "registered $($TaskNames -join ', ') (at logon; health check every 5 min)"
    }
    "remove-task" {
        foreach ($n in $TaskNames) {
            if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
                Unregister-ScheduledTask -TaskName $n -Confirm:$false
                Write-Host "removed $n"
            }
        }
    }
}
