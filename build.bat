@echo off
rem Build "BA Player.exe" (window) and "BA Player CLI.exe" (console) in this folder with PyInstaller.
rem Needs Python 3.9+ for Windows (python.org or the Microsoft Store), not MSYS2's python.
setlocal
cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
  echo Python for Windows not found. Install it from https://www.python.org/downloads/windows/
  echo and tick "Add python.exe to PATH", then run build.bat again.
  exit /b 1
)

echo == Installing / updating PyInstaller
%PY% -m pip install --upgrade pyinstaller || exit /b 1

set COMMON=--noconfirm --clean --onefile --paths "%~dp0src" --hidden-import ba_probe --hidden-import ba_player_cli --hidden-import combine_reports --add-data "%~dp0app-scaler;app-scaler" --add-data "%~dp0ba_player.ico;." --icon "%~dp0ba_player.ico" --distpath "%~dp0." --workpath "%~dp0build" --specpath "%~dp0build"

echo == Building "BA Player.exe" (window)
%PY% -m PyInstaller %COMMON% --windowed --name "BA Player" "%~dp0src\ba_player.py" || exit /b 1

echo == Building "BA Player CLI.exe" (console)
%PY% -m PyInstaller %COMMON% --console --name "BA Player CLI" "%~dp0src\ba_player_cli.py" || exit /b 1

echo.
echo Built "%~dp0BA Player.exe" and "%~dp0BA Player CLI.exe"
echo   BA Player.exe: open a report, set the AC-4 ffmpeg, Start. Or drop a report onto it.
echo   "BA Player CLI.exe" report.html [--ffmpeg-ac4 path] [--port N]: the same without a window.
endlocal
