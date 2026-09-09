$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Create .venv and install requirements first.' }
Start-Process -FilePath $pythonExe -ArgumentList ('"' + (Join-Path $PSScriptRoot 'launcher.py') + '"') -WorkingDirectory $projectDir -WindowStyle Hidden
