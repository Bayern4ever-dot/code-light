@echo off
setlocal
chcp 65001 >nul

echo ============================================================
echo  code-light PyInstaller build script
echo ============================================================

REM --- Configuration ---
set PROJECT_ROOT=%~dp0..
set SPEC_FILE=%PROJECT_ROOT%\build_specs\code-light.spec
set RELEASE_DIR=%PROJECT_ROOT%\release\code-light-0.2.0
set WORK_DIR=%TEMP%\code-light-build-work
set DIST_DIR=%TEMP%\code-light-build-dist

REM --- Use scho conda environment ---
call conda activate scho
if errorlevel 1 (
    echo ERROR: Failed to activate conda environment 'scho'
    pause
    exit /b 1
)
echo Using Python: %CONDA_PREFIX%\python.exe
%CONDA_PREFIX%\python.exe --version

REM --- Install/upgrade PyInstaller in scho env ---
echo.
echo [1/4] Ensuring PyInstaller is installed...
pip install --quiet pyinstaller
if errorlevel 1 (
    echo ERROR: Failed to install PyInstaller
    pause
    exit /b 1
)

REM --- Clean old build artifacts ---
echo.
echo [2/4] Cleaning old build artifacts...
if exist "%WORK_DIR%" rmdir /s /q "%WORK_DIR%"
if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%"
if exist "%PROJECT_ROOT%\dist" rmdir /s /q "%PROJECT_ROOT%\dist"
if exist "%PROJECT_ROOT%\build" rmdir /s /q "%PROJECT_ROOT%\build"

REM --- Run PyInstaller ---
echo.
echo [3/4] Running PyInstaller...
%CONDA_PREFIX%\python.exe -m PyInstaller ^
    "%SPEC_FILE%" ^
    --workpath "%WORK_DIR%" ^
    --distpath "%DIST_DIR%" ^
    --noconfirm ^
    --log-level WARN

if errorlevel 1 (
    echo ERROR: PyInstaller build failed
    pause
    exit /b 1
)

REM --- Copy to release directory ---
echo.
echo [4/4] Copying to release directory...
if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%"

robocopy "%DIST_DIR%\code-light" "%RELEASE_DIR%" /E /NFL /NDL /NJH /NJS /NC /NS /IS
if %ERRORLEVEL% GEQ 8 (
    echo ERROR: robocopy failed with code %ERRORLEVEL%
    pause
    exit /b 1
)

REM Copy launcher and README
copy /y "%PROJECT_ROOT%\packaging\run_code_light.bat" "%RELEASE_DIR%\run_code_light.bat" >nul
copy /y "%PROJECT_ROOT%\packaging\README.txt" "%RELEASE_DIR%\README.txt" >nul

echo.
echo ============================================================
echo  Build complete!
echo  Release: %RELEASE_DIR%
echo ============================================================
pause
