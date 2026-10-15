param(
    [Parameter(Mandatory=$true)][string]$EvidenceRoot,
    [Parameter(Mandatory=$true)][string]$CatalogueSha256
)
$ErrorActionPreference='Stop'
$workspace=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $workspace
$env:PYTHONPATH=(Join-Path $workspace 'src')+';'+(Join-Path $workspace 'src/tools')
$report=Join-Path $EvidenceRoot 'Reports/Video_Export'
[void](New-Item -ItemType Directory -Path $report -Force)
$stamp=Get-Date -Format 'yyyyMMddTHHmmss'
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class VideoExportAwake { [DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint flags); }'
[void][VideoExportAwake]::SetThreadExecutionState(2147483649)
try {
    & (Join-Path $workspace 'src/.venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'town10_video_export.py') run --root $EvidenceRoot --catalogue-sha256 $CatalogueSha256 1> (Join-Path $report "controller_$stamp.stdout.log") 2> (Join-Path $report "controller_$stamp.stderr.log")
    $exportExit=$LASTEXITCODE
} finally {
    [void][VideoExportAwake]::SetThreadExecutionState(2147483648)
}
exit $exportExit
