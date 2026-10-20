@echo off
setlocal
cd /d "%~dp0"
echo Installing Print Desk, please wait...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_client.ps1"
if errorlevel 1 (
  echo.
  echo Installation failed. See the error above.
) else (
  echo.
  echo Installation completed. Use the desktop shortcut.
)
echo.
pause
endlocal
