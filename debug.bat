@echo off
rem Run with a visible console so startup errors are printed live.
rem (Normal launch via "Rainette Music.exe" / start.bat is windowless; any crash
rem  is still written to rainette-music.log.)
cd /d "%~dp0"
python "%~dp0main.py"
echo.
echo --- Rainette Music exited. Press any key to close. ---
pause >nul
