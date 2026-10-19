@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up... this takes a minute.
  %PY% -m venv .venv || (echo Python 3.9+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -q --upgrade -r requirements.txt
".venv\Scripts\python.exe" app.py
pause
