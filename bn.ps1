# bn: the render node's three commands.
#   bn status   progress, time left, uploads, disk, GPU, last error
#   bn pause    finish the frame in progress, then stay paused (run twice to stop it at once)
#   bn resume   end a pause
# This file lives in <root>\app\. bin\bn.cmd calls it, so `bn status` works in any PowerShell or cmd window.
param(
    [Parameter(Position = 0)]
    [string]$Command = 'status'
)

$AppDir = $PSScriptRoot
if ($env:BN_RENDER_ROOT) {
    $Root = $env:BN_RENDER_ROOT
} else {
    $Root = Split-Path -Parent $AppDir
}
$Python = Join-Path $Root 'venv\Scripts\python.exe'
$Name = $Command.ToLower()

if (@('status', 'pause', 'resume') -notcontains $Name) {
    Write-Host 'Usage: bn status | bn pause | bn resume'
    exit 2
}
if (-not (Test-Path $Python)) {
    Write-Host "The render node is not set up on this PC: $Python is missing. Run setup.ps1 first."
    exit 1
}

$env:BN_RENDER_ROOT = $Root
$env:PYTHONIOENCODING = 'utf-8'
Push-Location $AppDir
try {
    & $Python -m bnrender $Name
    $Code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $Code
