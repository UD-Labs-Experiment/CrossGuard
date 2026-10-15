param([int]$Cars=12,[int]$People=20,[int]$Seed=7,[switch]$VerifyUI,[switch]$MockDemo,[ValidateSet("autonomous","retrofit")][string]$Receiver="autonomous")
$ErrorActionPreference='Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
}
$env:PYTHONPATH=$PSScriptRoot
$env:PYTHONDONTWRITEBYTECODE='1'
$viewerArgs = @('-m','crossguard.viewer','--cars',"$Cars",'--people',"$People",'--seed',"$Seed",'--receiver',$Receiver)
if ($VerifyUI) { $viewerArgs += '--verify-ui' }
if ($MockDemo) { $viewerArgs += '--mock-demo' }
& $taskPython @viewerArgs
if ($LASTEXITCODE -ne 0) { throw "Viewer exited with code $LASTEXITCODE" }
