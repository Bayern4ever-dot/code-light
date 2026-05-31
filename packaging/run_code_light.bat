@echo off
setlocal
cd /d "%~dp0"
start "" "%~dp0code-light.exe"
if errorlevel 1 (
  echo code-light failed to start. > "%~dp0last_error.log"
  echo Exit code: %ERRORLEVEL% >> "%~dp0last_error.log"
  pause
)
