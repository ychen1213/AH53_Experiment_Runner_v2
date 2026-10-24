# Start the local converter only; this does not start Lumerical.
$ErrorActionPreference = 'Stop'
$converterProject = Split-Path -Parent $PSScriptRoot
$converterEntry = Join-Path $PSScriptRoot 'lsf_config.py'
$converterRuntime = Join-Path $converterProject 'configs\local_runtime.json'
$converterPython = $null

if ($env:AH53_PYTHON) {
    $converterPython = $env:AH53_PYTHON
} elseif (Test-Path -LiteralPath $converterRuntime) {
    $converterRuntimeConfig = Get-Content -LiteralPath $converterRuntime -Raw -Encoding UTF8 | ConvertFrom-Json
    $converterPython = $converterRuntimeConfig.python_executable
}

if (-not $converterPython -or -not (Test-Path -LiteralPath $converterPython -PathType Leaf)) {
    $converterInstalled = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($converterInstalled -and $converterInstalled.Source -notlike '*\WindowsApps\*') {
        $converterPython = $converterInstalled.Source
    } else {
        Write-Host 'Python was not found. Set python_executable in configs/local_runtime.json,'
        Write-Host 'or set AH53_PYTHON to a Python executable with Tkinter.'
        exit 1
    }
}

Set-Location -LiteralPath $converterProject
$env:PYTHONUTF8 = '1'
& $converterPython $converterEntry gui
exit $LASTEXITCODE
