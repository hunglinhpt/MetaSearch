#!/usr/bin/env pwsh
# MetaSearch Lite manager (WP-14): start|stop|status on 127.0.0.1:8000
# Usage: metasearch-lite.ps1 start|stop|status
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot | Split-Path | Split-Path  # scripts/windows/ -> repo root
$ApiDir = Join-Path $Root "api"
$Python = Join-Path $ApiDir ".venv\Scripts\python.exe"
$PidFile = Join-Path $ApiDir ".lite_pid"
$LogFile = Join-Path $ApiDir ".lite.log"
$Port = 8000

function Get-LiteProcess {
    if (Test-Path $PidFile) {
        $pid = Get-Content $PidFile -ErrorAction SilentlyContinue
        $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -match "python") { return $proc }
    }
    return $null
}

switch ($Action) {
    "start" {
        $existing = Get-LiteProcess
        if ($existing) {
            Write-Host "Already running (PID $($existing.Id))"
            exit 0
        }
        if (-not (Test-Path $Python)) {
            Write-Error "venv not found at $Python — run: cd api && python -m venv .venv && .venv\Scripts\pip install -e '.[dev]'"
            exit 1
        }
        $env:API_HOST = "127.0.0.1"
        $env:AUTH_ENABLED = "true"
        $proc = Start-Process -FilePath $Python `
            -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port" `
            -WorkingDirectory $ApiDir -WindowStyle Hidden -RedirectStandardOutput $LogFile -RedirectStandardError "$LogFile.err" `
            -PassThru
        Set-Content $PidFile $proc.Id
        Start-Sleep -Seconds 3
        try {
            $health = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 5 -UseBasicParsing
            Write-Host "Started (PID $($proc.Id)) — healthz: $($health.StatusCode)"
        } catch {
            Write-Warning "Started but healthz not responding yet (check $LogFile.err)"
        }
    }
    "stop" {
        $proc = Get-LiteProcess
        if ($proc) {
            Stop-Process -Id $proc.Id -Force
            Write-Host "Stopped (PID $($proc.Id))"
        } else {
            Write-Host "Not running"
        }
        Remove-Item $PidFile -ErrorAction SilentlyContinue
    }
    "status" {
        $proc = Get-LiteProcess
        if ($proc) {
            try {
                $health = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/readyz" -TimeoutSec 5 -UseBasicParsing
                Write-Host "Running (PID $($proc.Id)) — readyz: $($health.Content)"
            } catch {
                Write-Host "Running (PID $($proc.Id)) — readyz unreachable"
            }
        } else {
            Write-Host "Not running"
        }
    }
}
