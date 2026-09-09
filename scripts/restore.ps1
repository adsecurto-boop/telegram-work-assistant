param([Parameter(Mandatory=$true)][string]$BackupPath)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
& '.\.venv\Scripts\python.exe' 'scripts\restore.py' $BackupPath
exit $LASTEXITCODE
