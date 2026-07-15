@echo off
setlocal
rem Build the thin Rainette Music launcher with the bundled Kagebana icon.
cd /d "%~dp0" || exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name "Rainette Music" --icon "%~dp0web\assets\rainette-icon.ico" --distpath "%~dp0." --workpath "%~dp0build" --specpath "%~dp0build" "%~dp0launcher.py"
if errorlevel 1 exit /b 1
echo.
echo Done. "Rainette Music.exe" is in this folder.
exit /b 0
