# Start everything for local testing: dashboard backend, dashboard frontend, and the
# Cloudflare quick tunnel that lets Cartesia send call events to the backend.
# (Calls run on Cartesia's cloud; there is no local agent worker any more.)
#
#   .\start_all.ps1            # production frontend (builds once if needed)
#   .\start_all.ps1 -Dev       # frontend with hot reload (uses more RAM)
#   .\start_all.ps1 -Rebuild   # force a fresh frontend build
#   .\start_all.ps1 -NoTunnel  # no public URL (status/transcript then come from polling Cartesia)
#   .\start_all.ps1 -NoRemote  # no dashboard tunnel for the supervisor's laptop (it only starts once a login is set)
#
# Each part opens in its own window (logs visible there). Stop everything with:
#   .\stop_all.ps1             (or close the three windows)
#
# Dashboard: http://127.0.0.1:3000   API: http://127.0.0.1:8000

param(
    [switch]$Dev,
    [switch]$Rebuild,
    [switch]$NoTunnel,
    [switch]$NoRemote,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$PidFile = Join-Path $Root ".cache\running.json"
. (Join-Path $Root "setup_env.ps1") | Out-Null

if (Test-Path $PidFile) {
    Write-Warning "Something may already be running (found .cache\running.json). Run .\stop_all.ps1 first."
    return
}

function Start-Part([string]$Title, [string]$WorkDir, [string]$Command, [hashtable]$ExtraEnv = @{}) {
    $envLines = ($ExtraEnv.GetEnumerator() | ForEach-Object { "`$env:$($_.Key) = '$($_.Value)';" }) -join " "
    $script = "`$Host.UI.RawUI.WindowTitle = 'DialMind - $Title'; . '$Root\setup_env.ps1' | Out-Null; $envLines Set-Location '$WorkDir'; $Command"
    $p = Start-Process powershell -ArgumentList "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $script -PassThru
    Write-Host ("  {0,-10} pid {1}" -f $Title, $p.Id)
    return $p.Id
}

function Wait-Url([string]$Url, [int]$Seconds) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        try { Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true } catch { Start-Sleep 1 }
    }
    return $false
}

$frontend = Join-Path $Root "dashboard\frontend"
if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
    Write-Host "Installing frontend packages..."
    Push-Location $frontend; npm install --no-audit --loglevel=error; Pop-Location
}
$buildId = Join-Path $frontend ".next\BUILD_ID"
if (-not $Dev -and -not $Rebuild -and (Test-Path $buildId)) {
    # Rebuild when the frontend source changed since the last build (new pages, next.config.ts).
    $built = (Get-Item $buildId).LastWriteTime
    $newer = Get-ChildItem -Path "$frontend\app", "$frontend\components", "$frontend\lib", "$frontend\next.config.ts" -Recurse -File |
        Where-Object { $_.LastWriteTime -gt $built } | Select-Object -First 1
    if ($newer) { Write-Host "Frontend changed since the last build ($($newer.Name))."; $Rebuild = $true }
}
if (-not $Dev -and ($Rebuild -or -not (Test-Path $buildId))) {
    Write-Host "Building the frontend (one time, ~1 min)..."
    Push-Location $frontend; npm run build | Out-Host; $ok = $LASTEXITCODE -eq 0; Pop-Location
    if (-not $ok) { throw "Frontend build failed." }
}

Write-Host "Starting:"
$pids = @{}
$pids.backend = Start-Part "Backend" "$Root\dashboard\backend" "python -m uvicorn app.main:app --host 127.0.0.1 --port 8000" @{ DASHBOARD_DEV = "1" }
if (-not $NoTunnel) {
    $cloudflared = if ($env:CLOUDFLARED_PATH) { $env:CLOUDFLARED_PATH } else { (Get-Command cloudflared -ErrorAction SilentlyContinue).Source }
    if ($cloudflared -and (Test-Path $cloudflared)) {
        # Quick tunnel (random https://*.trycloudflare.com URL, no Cloudflare account needed).
        # The backend reads the URL from the metrics port and points the Cartesia webhook at it.
        $pids.tunnel = Start-Part "Tunnel" $Root "& '$cloudflared' tunnel --no-autoupdate --metrics 127.0.0.1:20241 --url http://127.0.0.1:8000"
        # Second quick tunnel -> the dashboard, for the supervisor's laptop. Only when a login is set
        # (manage_users.py, or the old single login in .env); the URL is shown on the Supervisor page.
        $envFile = Join-Path $Root ".env"
        $loginSet = (Test-Path (Join-Path $Root "dashboard_users.json")) -or ((Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^DASHBOARD_PASSWORD_HASH=\S' -Quiet))
        if ($loginSet -and -not $NoRemote) {
            $pids.dashtunnel = Start-Part "Dash tunnel" $Root "& '$cloudflared' tunnel --no-autoupdate --metrics 127.0.0.1:20242 --url http://127.0.0.1:3000"
        }
    } else {
        Write-Warning "cloudflared not found at $cloudflared - starting without the tunnel."
    }
}
$frontCmd = if ($Dev) { "npm run dev" } else { "npm run start" }
$pids.frontend = Start-Part "Frontend" $frontend $frontCmd

$pids | ConvertTo-Json | Set-Content -Encoding utf8 $PidFile

Write-Host "Waiting for the backend and frontend..."
$apiUp = Wait-Url "http://127.0.0.1:8000/api/config" 60
$webUp = Wait-Url "http://127.0.0.1:3000" 90
if ($apiUp -and $webUp) {
    Write-Host "`nReady: http://127.0.0.1:3000/leads" -ForegroundColor Green
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:3000/leads" }
} else {
    Write-Warning "Not everything came up (backend: $apiUp, frontend: $webUp). Check the windows for errors."
}
Write-Host "Stop everything with: .\stop_all.ps1"
