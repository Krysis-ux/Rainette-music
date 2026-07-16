@echo off
rem Dev-only: run from source with a visible console so startup errors print live.
rem (The shipped app is RainetteMusic.exe from the installer; start.bat is the
rem  windowless source launch. Any crash is also written to rainette-music.log.)
cd /d "%~dp0"
python "%~dp0main.py"
echo.
echo --- Rainette Music exited. Press any key to close. ---
pause >nul
