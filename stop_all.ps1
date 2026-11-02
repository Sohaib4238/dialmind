# Stop everything started by start_all.ps1 (backend, frontend, tunnel) and close their windows.
#   .\stop_all.ps1

$Root = $PSScriptRoot
$PidFile = Join-Path $Root ".cache\running.json"

if (-not (Test-Path $PidFile)) {
    Write-Host "Nothing recorded as running (no .cache\running.json)."
} else {
    $pids = Get-Content $PidFile -Raw | ConvertFrom-Json
    foreach ($name in $pids.PSObject.Properties.Name) {
        $id = $pids.$name
        if (Get-Process -Id $id -ErrorAction SilentlyContinue) {
            # /T stops the whole tree: the window plus python / node child processes.
            taskkill /PID $id /T /F | Out-Null
            Write-Host ("Stopped {0,-9} (pid {1})" -f $name, $id)
        } else {
            Write-Host ("{0,-9} was not running" -f $name)
        }
    }
    Remove-Item $PidFile
}

# Safety net: anything still listening on the dashboard ports.
foreach ($port in 8000, 3000) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
        Write-Host "Freed port $port (pid $($_.OwningProcess))"
    }
}
