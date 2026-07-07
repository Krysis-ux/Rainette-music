@echo off
rem Zero-build launch: run the app with the system Python (windowed, detached).
cd /d "%~dp0"
start "" pythonw "%~dp0main.py"
