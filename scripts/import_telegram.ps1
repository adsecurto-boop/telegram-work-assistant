param(
    [Parameter(Mandatory=$true,Position=0)][string[]]$Path,
    [string[]]$Owner,
    [switch]$Apply,
    [int]$Limit
)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Create .venv and install requirements first.' }
$arguments = @((Join-Path $PSScriptRoot 'import_telegram.py')) + $Path
if ($Apply) { $arguments += '--apply' }
foreach ($alias in $Owner) { $arguments += @('--owner',$alias) }
if ($Limit -gt 0) { $arguments += @('--limit',$Limit) }
& $pythonExe @arguments
exit $LASTEXITCODE
