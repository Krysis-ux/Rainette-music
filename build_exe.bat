@echo off
rem Build "Rainette Music.exe" — a thin launcher that starts main.py with the
rem system Python. Rebuild only if you change launcher.py.
cd /d "%~dp0"
pyinstaller --onefile --noconsole --name "Rainette Music" --distpath "%~dp0" --workpath "%~dp0build" --specpath "%~dp0build" launcher.py
echo.
echo Done. "Rainette Music.exe" is in this folder.
