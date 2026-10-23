<#
Run from PowerShell 7, passing native flags in an explicit array:
  .\tools\run_logged.ps1 -LogPath local_validation\tests.log `
    -Program 'D:\Ansys Inc\v252\Lumerical\python\python.exe' `
    -CommandArguments @('-m', 'unittest', 'discover', '-s', 'tests', '-v')
#>
param(
    [Parameter(Mandatory = $true)][string]$LogPath,
    [Parameter(Mandatory = $true)][string]$Program,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$CommandArguments
)

# Keep exact argument boundaries and the native program's exit code.
$ErrorActionPreference = 'Stop'
$resolvedLog = [System.IO.Path]::GetFullPath($LogPath)
$logDirectory = [System.IO.Path]::GetDirectoryName($resolvedLog)
[System.IO.Directory]::CreateDirectory($logDirectory) | Out-Null
$commandRecord = [ordered]@{
    timestamp = (Get-Date).ToString('o')
    working_directory = (Get-Location).Path
    program = $Program
    arguments = @($CommandArguments)
}
$commandRecord | ConvertTo-Json -Compress | Out-File -LiteralPath $resolvedLog -Encoding utf8
$priorPythonUtf8 = $env:PYTHONUTF8
$priorPythonEncoding = $env:PYTHONIOENCODING
$priorConsoleEncoding = [Console]::OutputEncoding
try {
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    & $Program @CommandArguments 2>&1 | Tee-Object -FilePath $resolvedLog -Append
    $programExitCode = $LASTEXITCODE
}
finally {
    $env:PYTHONUTF8 = $priorPythonUtf8
    $env:PYTHONIOENCODING = $priorPythonEncoding
    [Console]::OutputEncoding = $priorConsoleEncoding
}
"exit_code=$programExitCode" | Out-File -LiteralPath $resolvedLog -Append -Encoding utf8
exit $programExitCode
