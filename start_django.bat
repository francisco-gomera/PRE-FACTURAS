@echo off
cd /d "%~dp0"
if exist "venv_local\Scripts\activate.bat" (
    call venv_local\Scripts\activate.bat
) else (
    call venv\Scripts\activate.bat
)
python run_server.py
pause

