<#
Use the verified local Python; no solver starts with the default check command.
Examples:
  .\launch_local.ps1
  .\launch_local.ps1 -Task quick
  .\launch_local.ps1 -Task run -RunnerArguments @('--config','configs/quick_check.json','--limit','1','--visible')
  .\launch_local.ps1 -Task overnight-check
  .\launch_local.ps1 -Task overnight
#>
param(
    [ValidateSet('check','plan','run','report','quick','physics','overnight-check','overnight')]
    [string]$Task = 'check',
    [string[]]$RunnerArguments = @()
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
Set-Location -LiteralPath $PSScriptRoot

function Find-LocalPython {
    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($env:AH53_PYTHON) { $candidates.Add($env:AH53_PYTHON) }
    $runtimeFile = Join-Path $PSScriptRoot 'configs\local_runtime.json'
    if (Test-Path -LiteralPath $runtimeFile -PathType Leaf) {
        $runtime = Get-Content -LiteralPath $runtimeFile -Raw -Encoding utf8 | ConvertFrom-Json
        if ($runtime.python_executable) { $candidates.Add([string]$runtime.python_executable) }
    }
    $candidates.Add('D:\Ansys Inc\v252\Lumerical\python\python.exe')
    foreach ($commandName in @('python.exe', 'python3.exe')) {
        foreach ($candidateCommand in @(Get-Command $commandName -All -ErrorAction SilentlyContinue)) {
            if ($candidateCommand.Source -and $candidateCommand.Source -notmatch '[\\/]WindowsApps[\\/]') {
                $candidates.Add($candidateCommand.Source)
            }
        }
    }
    $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pythonLauncher -and $pythonLauncher.Source -notmatch '[\\/]WindowsApps[\\/]') {
        $launchedPython = @(& $pythonLauncher.Source -3 -c 'import sys; print(sys.executable)' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $launchedPython) { $candidates.Add([string]$launchedPython[-1]) }
    }
    foreach ($candidatePath in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidatePath -PathType Leaf)) { continue }
        $probeOutput = & $candidatePath -c 'import sys, numpy, matplotlib; print(sys.executable)' 2>&1
        if ($LASTEXITCODE -eq 0) { return [System.IO.Path]::GetFullPath($candidatePath) }
        Write-Host "Python runtime check failed: $candidatePath"
        Write-Host ($probeOutput | Out-String)
    }
    throw 'No usable Python with numpy and matplotlib found. Set configs/local_runtime.json python_executable or AH53_PYTHON to a working interpreter.'
}

try {
    $pythonPath = Find-LocalPython
    $logDirectory = Join-Path $PSScriptRoot 'logs'
    [System.IO.Directory]::CreateDirectory($logDirectory) | Out-Null
    $logPath = Join-Path $logDirectory ("{0}_{1}.log" -f $Task, (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
    Write-Host "PYTHON: $pythonPath"
    Write-Host "LOG: $logPath"

    function Invoke-LocalCommand([string[]]$NativeArguments) {
        $entry = [ordered]@{timestamp=(Get-Date).ToString('o'); python=$pythonPath; arguments=@($NativeArguments)}
        $entry | ConvertTo-Json -Compress | Out-File -LiteralPath $logPath -Append -Encoding utf8
        & $pythonPath @NativeArguments 2>&1 | Tee-Object -FilePath $logPath -Append | Out-Host
        $nativeExitCode = $LASTEXITCODE
        "exit_code=$nativeExitCode" | Out-File -LiteralPath $logPath -Append -Encoding utf8
        return $nativeExitCode
    }

    switch ($Task) {
        'quick' {
            $code = Invoke-LocalCommand -NativeArguments @('run_experiments.py','plan','--config','configs/quick_check.json')
            if ($code -eq 0) {
                $code = Invoke-LocalCommand -NativeArguments @('run_experiments.py','run','--config','configs/quick_check.json','--limit','1','--visible')
                $reportCode = Invoke-LocalCommand -NativeArguments @('run_experiments.py','report','--config','configs/quick_check.json')
                if ($code -eq 0) { $code = $reportCode }
            }
        }
        'physics' { $code = Invoke-LocalCommand -NativeArguments @('tools/validate_physics.py') }
        'overnight-check' { $code = Invoke-LocalCommand -NativeArguments @('tools/run_validated_overnight.py') }
        'overnight' { $code = Invoke-LocalCommand -NativeArguments @('tools/run_validated_overnight.py','--execute') }
        default {
            $code = Invoke-LocalCommand -NativeArguments (@('run_experiments.py', $Task) + $RunnerArguments)
        }
    }
    exit $code
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
