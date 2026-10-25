# Installs Agent Doorbell for all VS Code Copilot sessions of this Windows user.
#  - writes the user-level Copilot hook file (~/.copilot/hooks/agent-doorbell.json)
#  - installs the helper VS Code extension
#  - registers the "Agent Doorbell" scheduled task: starts the supervisor at logon and relaunches
#    it every 5 minutes if it was stopped (the supervisor restarts a crashed or hung bridge)
#  - (re)starts everything
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python -ErrorAction Stop).Source
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }
$bridgePy = Join-Path $here 'bridge.py'
$taskName = 'Agent Doorbell'

Push-Location $here
try {
    & $py -m pip install --quiet --disable-pip-version-check -r requirements.txt
    & $py -c "import bridge; bridge.load_config()"
} finally { Pop-Location }
$cfg = Get-Content (Join-Path $here 'config.json') -Raw | ConvertFrom-Json

# Stop anything already running (supervisor first, so it can't restart the bridge).
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $taskName }
$running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($bridgePy) }
$running | Sort-Object { -not $_.CommandLine.Contains('supervise') } | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "Stopped PID $($_.ProcessId)"
}

# Copilot hooks.
$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { Join-Path $env:USERPROFILE '.copilot' }
$hooksDir = Join-Path $copilotHome 'hooks'
New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null
Remove-Item (Join-Path $hooksDir 'whatsapp-hitl.json') -ErrorAction SilentlyContinue  # name used by older versions
$hookPy = Join-Path $here 'hook.py'
function HookEntry([string]$evt, [int]$timeoutSec, [string]$matcher) {
    $e = [ordered]@{
        type       = 'command'
        powershell = "& '$py' '$hookPy' $evt"
        timeoutSec = $timeoutSec
    }
    if ($matcher) { $e.matcher = $matcher }
    return ,@($e)
}
$hooks = [ordered]@{
    version = 1
    hooks   = [ordered]@{
        permissionRequest = HookEntry 'permissionRequest' ([int]($cfg.approval_wait_minutes * 60 + 120)) ''
        preToolUse        = HookEntry 'preToolUse' ([int]($cfg.question_wait_minutes * 60 + 120)) 'ask_user'
        agentStop         = HookEntry 'agentStop' ([int]($cfg.followup_wait_minutes * 60 + 120)) ''
        notification      = HookEntry 'notification' 30 'permission_prompt|elicitation_dialog'
    }
}
$hookFile = Join-Path $hooksDir 'agent-doorbell.json'
[IO.File]::WriteAllText($hookFile, ($hooks | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding $false))
Write-Host "Hook file written: $hookFile"

# Helper VS Code extension: applies phone answers to prompts that are already showing in VS Code.
Push-Location $here
try { $vsix = (& $py build_vsix.py).Trim() } finally { Pop-Location }
$code = Get-Command code -ErrorAction SilentlyContinue
if ($code) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'  # 'code' writes harmless warnings to stderr
    & $code.Source --uninstall-extension local.copilot-phone-bridge 2>&1 | Out-Null  # name used by older versions
    & $code.Source --install-extension $vsix --force 2>&1 | Out-Null
    $ErrorActionPreference = $prev
    Write-Host "Helper extension installed: $vsix"
} else {
    Write-Warning "'code' CLI not found. Install $vsix manually (Extensions view > ... > Install from VSIX)."
}

# Autostart with crash recovery.
$startup = [Environment]::GetFolderPath('Startup')
Remove-Item (Join-Path $startup 'Copilot WhatsApp Bridge.lnk') -ErrorAction SilentlyContinue  # older versions
$fallbackLnk = Join-Path $startup 'Agent Doorbell.lnk'
$user = "$env:USERDOMAIN\$env:USERNAME"
$useTask = $true
try {
    $action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$bridgePy`" supervise" -WorkingDirectory $here
    $atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
    $every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $atLogon, $every5 -Settings $settings `
        -Principal $principal -Description 'Agent Doorbell: phone approvals for VS Code Copilot agents' -Force | Out-Null
    Remove-Item $fallbackLnk -ErrorAction SilentlyContinue
    Write-Host "Scheduled task '$taskName' registered (starts at logon, checks every 5 minutes)."
} catch {
    $useTask = $false
    Write-Warning "Couldn't register a scheduled task ($($_.Exception.Message)). Using a Startup shortcut instead."
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($fallbackLnk)
    $sc.TargetPath = $pyw
    $sc.Arguments = "`"$bridgePy`" supervise"
    $sc.WorkingDirectory = $here
    $sc.Description = 'Agent Doorbell supervisor'
    $sc.Save()
    Write-Host "Startup shortcut: $fallbackLnk"
}

# Start now.
if ($useTask) { Start-ScheduledTask -TaskName $taskName }
else { Start-Process -FilePath $pyw -ArgumentList "`"$bridgePy`" supervise" -WorkingDirectory $here -WindowStyle Hidden }
$h = $null
for ($i = 0; $i -lt 30 -and -not $h; $i++) {
    Start-Sleep -Milliseconds 700
    try { $h = Invoke-RestMethod "http://127.0.0.1:$($cfg.port)/health" -TimeoutSec 3 } catch { }
}
if (-not $h) { throw "The bridge didn't start. See supervisor.log and bridge.log in $here" }
Write-Host "Bridge running: $($h | ConvertTo-Json -Compress)"
if ($h.channel -eq 'dry-run') {
    Write-Warning "No phone channel configured yet (dry-run). Run: python bridge.py setup-telegram   then re-run this script."
}
