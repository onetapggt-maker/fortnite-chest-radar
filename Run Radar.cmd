@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Radar dependencies are missing. Open the project in Codex to restore them.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" radar.py
