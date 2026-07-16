@echo off
rem Dev-only source launch: run the app with the system Python (windowed, detached).
rem End users run the installed RainetteMusic.exe instead. Task Manager will show
rem "pythonw.exe" here because this genuinely is the interpreter's process.
cd /d "%~dp0"
start "" pythonw "%~dp0main.py"
