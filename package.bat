@echo off
rem Pack BA Player for sharing: BA_Player.zip with the .exe files, documents, source,
rem extension and sample report. Leaves out your settings, logs and the build folder.
rem Run build.bat first.
setlocal
cd /d "%~dp0"

if not exist "BA Player.exe" (
  echo "BA Player.exe" not found. Run build.bat first.
  exit /b 1
)
if not exist "BA Player CLI.exe" (
  echo "BA Player CLI.exe" not found. Run build.bat first.
  exit /b 1
)

set "STAGE=%TEMP%\BA_Player_pkg"
set "OUT=%STAGE%\BA_Player"
if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%OUT%\src" "%OUT%\app-scaler" "%OUT%\reports" || exit /b 1

copy /y "BA Player.exe" "%OUT%\" >nul || exit /b 1
copy /y "BA Player CLI.exe" "%OUT%\" >nul || exit /b 1
for %%f in (README.md LICENSE.txt THIRD-PARTY-NOTICES.md ba_player.ico build.bat package.bat) do (
  copy /y "%%f" "%OUT%\" >nul || exit /b 1
)
for %%f in (ba_player.py ba_player_cli.py ba_probe.py combine_reports.py) do (
  copy /y "src\%%f" "%OUT%\src\" >nul || exit /b 1
)
for %%f in (manifest.json content.js audio.js icon16.png icon32.png icon48.png icon128.png) do (
  copy /y "app-scaler\%%f" "%OUT%\app-scaler\" >nul || exit /b 1
)
copy /y "reports\sample.html" "%OUT%\reports\" >nul || exit /b 1

if exist "BA_Player.zip" del /q "BA_Player.zip"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path '%OUT%' -DestinationPath '%~dp0BA_Player.zip' -CompressionLevel Optimal" || exit /b 1
rmdir /s /q "%STAGE%"

echo.
echo Made %~dp0BA_Player.zip
echo   It unzips to a BA_Player folder: run "BA Player.exe" from there.
endlocal
