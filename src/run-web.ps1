param([int]$Port=8000,[ValidateSet('autonomous','retrofit')][string]$Receiver='autonomous',
      [ValidateSet('single','lead-only','cooperative')][string]$GroupMode='cooperative',
      [ValidateSet('radio','cellular','optical','hybrid')][string]$CommunicationProfile='hybrid')
$ErrorActionPreference='Stop'
$taskPython=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $taskPython)){throw 'Install the src Python environment first.'}
$taskWeights=Join-Path $PSScriptRoot 'models\yolo11n.onnx'
if(-not(Test-Path -LiteralPath $taskWeights)){throw 'YOLO ONNX model is missing; see src/models/MODEL_PROVENANCE.json.'}
$env:PYTHONPATH=$PSScriptRoot
Write-Host "Dashboard: http://127.0.0.1:$Port (CARLA must be running on port 2000)"
& $taskPython -m crossguard.webserver --weights $taskWeights --device gpu --port $Port --receiver $Receiver --group-mode $GroupMode --communication-profile $CommunicationProfile
if($LASTEXITCODE -ne 0){throw "Browser bridge exited with $LASTEXITCODE"}
