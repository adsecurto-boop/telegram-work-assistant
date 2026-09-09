param([switch]$InstallAutostart)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Create .venv and install requirements first. See README.md.' }
Set-Location -LiteralPath $projectDir
& $pythonExe 'scripts\check_config.py'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($InstallAutostart) {
    $hiddenPython = Join-Path $projectDir '.venv\Scripts\pythonw.exe'
    $action = New-ScheduledTaskAction -Execute $hiddenPython -Argument ('"' + (Join-Path $projectDir 'bot.py') + '"') -WorkingDirectory $projectDir
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName 'Telegram Work Assistant' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Personal shift journal Telegram bot' -Force | Out-Null
    Write-Host 'Autostart registered. Start it in Task Scheduler, or sign in again.'
} else {
    Set-Location -LiteralPath $projectDir
    & $pythonExe bot.py
    exit $LASTEXITCODE
}
