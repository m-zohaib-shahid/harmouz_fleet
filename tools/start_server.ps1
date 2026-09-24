# tools/start_server.ps1 - (re)start the AegisFleet backend so it survives the shell.
#   Serves the REST API, the WebSocket feed AND the dashboard:
#       http://localhost:8000
#
# Usage:  powershell -ExecutionPolicy Bypass -File tools\start_server.ps1 [-Port 8000]

param([int]$Port = 8000)

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
$backend = Join-Path $root 'backend'
$log = Join-Path $root 'tools\server.log'

if (-not (Test-Path $py)) { Write-Host "python venv not found at $py" -ForegroundColor Red; exit 1 }

# stop whatever already owns the port
$pids = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).OwningProcess | Select-Object -Unique
foreach ($p in $pids) {
    Write-Host "stopping existing listener PID $p"
    Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 1

# Win32_Process.Create + cmd /c => fully detached from this shell, so closing the
# terminal (or the agent) cannot kill the server.
$inner = "`"$py`" -m uvicorn app.main:app --host 0.0.0.0 --port $Port > `"$log`" 2>&1"
$res = ([wmiclass]'Win32_Process').Create("cmd.exe /c $inner", $backend, $null)
if ($res.ReturnValue -ne 0) { Write-Host "failed to start (code $($res.ReturnValue))" -ForegroundColor Red; exit 1 }

Write-Host "uvicorn launched (PID $($res.ProcessId)); logs -> $log" -ForegroundColor Green

# wait for it to answer
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $h = Invoke-RestMethod "http://localhost:$Port/health" -TimeoutSec 3
        Write-Host "healthy: ships=$($h.ships) tick=$($h.tick) sim=$($h.sim_clock)" -ForegroundColor Green
        Write-Host ""
        Write-Host "  DASHBOARD  ->  http://localhost:$Port" -ForegroundColor Cyan
        $ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
               Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
               Select-Object -First 1).IPAddress
        if ($ip) { Write-Host "  LAN        ->  http://${ip}:$Port" -ForegroundColor Cyan }
        exit 0
    } catch {
        # still booting (A* plans 15 routes at startup)
    }
}
Write-Host "server did not become healthy - check $log" -ForegroundColor Red
exit 1
