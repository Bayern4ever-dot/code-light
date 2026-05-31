code-light 0.1.0 Windows build

How to run
1. Double-click run_code_light.bat, or run code-light.exe directly.
2. Keep code-light.exe and the _internal folder together.
3. The dashboard opens at http://127.0.0.1:7681 by default.

Notes
- This build was packaged with PyInstaller on Windows using the conda environment: scho.
- This is an onedir build. Do not copy only code-light.exe to another folder.
- Windows SmartScreen or antivirus may warn on unsigned executables.
- If launch fails from the batch file, check last_error.log in this folder.

Required local app data
- Claude data is read from %USERPROFILE%\.claude.
- Codex data and auth are read from %USERPROFILE%\.codex.
- code-light stores state under %USERPROFILE%\.code-light.
