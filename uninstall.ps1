# Removes Agent Doorbell's autostart, hooks and helper extension, and stops it.
# config.json (your bot token) is kept; delete the folder to remove everything.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bridgePy = Join-Path $here 'bridge.py'
$taskName = 'Agent Doorbell'

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed scheduled task '$taskName'"
}
$startup = [Environment]::GetFolderPath('Startup')
foreach ($lnk in 'Agent Doorbell.lnk', 'Copilot WhatsApp Bridge.lnk') {
    Remove-Item (Join-Path $startup $lnk) -ErrorAction SilentlyContinue
}

Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($bridgePy) } |
    Sort-Object { -not $_.CommandLine.Contains('supervise') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "Stopped PID $($_.ProcessId)" }

$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    Push-Location $here
    try { & $py.Source bridge.py uninstall-hooks | ForEach-Object { Write-Host $_ } } finally { Pop-Location }
}
$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { Join-Path $env:USERPROFILE '.copilot' }
foreach ($f in 'agent-doorbell.json', 'whatsapp-hitl.json') {
    Remove-Item (Join-Path $copilotHome "hooks\$f") -ErrorAction SilentlyContinue
}

$code = Get-Command code -ErrorAction SilentlyContinue
if ($code) {
    $ErrorActionPreference = 'Continue'
    foreach ($ext in 'local.agent-doorbell-helper', 'local.copilot-phone-bridge') {
        & $code.Source --uninstall-extension $ext 2>&1 | Out-Null
    }
}
Write-Host 'Agent Doorbell uninstalled. config.json is kept; delete the folder to remove everything.'
