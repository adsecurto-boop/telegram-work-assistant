param([switch]$RunTests)
$ErrorActionPreference = 'Continue'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
Set-Location -LiteralPath $projectDir
& $pythonExe 'scripts\check_config.py'
& $pythonExe 'scripts\check_database.py'
& $pythonExe -m pip check
Get-ScheduledTask -TaskName 'Telegram Work Assistant' -ErrorAction SilentlyContinue |
    Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName 'Telegram Work Assistant' -ErrorAction SilentlyContinue |
    Select-Object LastRunTime,LastTaskResult
Get-Content 'bot.log' -Tail 10 -ErrorAction SilentlyContinue
if ($RunTests) { & $pythonExe 'smoke_test.py' }
