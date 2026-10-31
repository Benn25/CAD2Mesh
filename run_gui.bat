@echo off
rem Launch the STEP -> USD converter GUI (no console window).
rem Fatal errors are shown in a dialog and logged to %~dp0error.log
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo No .venv found in "%~dp0"
    echo Create it with:  py -3.12 -m venv .venv
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "run_gui.py" %*
