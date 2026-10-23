@echo off
setlocal
cd /d "%~dp0"
where pwsh.exe >nul 2>&1
if errorlevel 1 (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_local.ps1" -Task overnight
) else (
  pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_local.ps1" -Task overnight
)
set "RUN_EXIT_CODE=%ERRORLEVEL%"
echo Overnight command finished with exit code %RUN_EXIT_CODE%. See logs and output_ah53_overnight.
exit /b %RUN_EXIT_CODE%
